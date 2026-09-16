#!/usr/bin/env bash
# End-to-end compose smoke test: brings up the *actual* docker-compose.yml
# services (built Dockerfiles, real env/secrets/volume wiring, not
# testcontainers-managed substitutes) -- timescaledb, grafana, mqtt-ingest,
# plus a disposable mosquitto broker from docker-compose.test.yml -- feeds
# them a small synthetic packet corpus over real MQTT, and confirms rows
# land in Postgres and the provisioned Grafana dashboard is reachable.
#
# Unlike tests/test_mqtt_ingest_replay.py and tests/test_grafana_provisioning.py
# (which drive each service's Python entrypoint in-process, or spin up
# individual containers directly via testcontainers), this is the only check
# that actually exercises `docker compose build`/`up` against this repo's own
# compose files and Dockerfiles end to end.
#
# Runs under an isolated compose project name and isolated host ports
# (15432/13000), and its own tests/smoke/regions.yaml -- never reads or
# touches a deployer's real .env, config/regions.yaml, or secrets/* content.
#
# Prerequisite: secrets/mqtt_password.txt must exist (its content is unused
# here -- mosquitto allows anonymous access -- but docker-compose.yml
# declares it as a required Compose secret file for mqtt-ingest):
#   cp secrets/mqtt_password.txt.sample secrets/mqtt_password.txt

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

PROJECT=meshtastic-db-smoke
GRAFANA_URL=http://localhost:13000
COMPOSE=(docker compose -p "$PROJECT" --env-file tests/smoke/smoke.env -f docker-compose.yml -f docker-compose.test.yml)

cleanup() {
  "${COMPOSE[@]}" down -v --remove-orphans
}
trap cleanup EXIT

if [[ ! -f secrets/mqtt_password.txt ]]; then
  echo "secrets/mqtt_password.txt is missing -- run:" >&2
  echo "  cp secrets/mqtt_password.txt.sample secrets/mqtt_password.txt" >&2
  exit 1
fi

wait_until() {
  local description="$1" timeout_seconds="$2" check="$3"
  local deadline=$((SECONDS + timeout_seconds))
  until eval "$check"; do
    if (( SECONDS > deadline )); then
      echo "timed out waiting for: $description" >&2
      return 1
    fi
    sleep 2
  done
}

psql_smoke() {
  "${COMPOSE[@]}" exec -T timescaledb psql -U postgres -d meshtastic -tA -c "$1"
}

echo "==> Building images"
"${COMPOSE[@]}" build mqtt-ingest

echo "==> Starting timescaledb, grafana, mosquitto, mqtt-ingest"
"${COMPOSE[@]}" up -d timescaledb grafana mosquitto mqtt-ingest

echo "==> Waiting for timescaledb"
wait_until "timescaledb ready" 60 '"${COMPOSE[@]}" exec -T timescaledb pg_isready -U postgres >/dev/null 2>&1'

echo "==> Waiting for mqtt-ingest to subscribe"
wait_until "mqtt-ingest subscribed to mosquitto" 30 '"${COMPOSE[@]}" logs mqtt-ingest 2>&1 | grep -q "connected, subscribed"'

echo "==> Publishing synthetic corpus (Telemetry, Position, NodeInfo) over MQTT"
"${COMPOSE[@]}" run --rm --no-deps mqtt-ingest python /tests/smoke/corpus_publisher.py --host mosquitto --port 1883 --region SMOKE

echo "==> Waiting for decoded rows to land in Postgres"
wait_until "metric rows for region SMOKE" 60 '[[ "$(psql_smoke "SELECT count(*) FROM metric WHERE region = '"'"'SMOKE'"'"'")" -gt 0 ]]'
wait_until "node_position_history row for region SMOKE" 30 '[[ "$(psql_smoke "SELECT count(*) FROM node_position_history WHERE region = '"'"'SMOKE'"'"'")" -gt 0 ]]'
wait_until "node_identity row for the smoke-test node" 30 '[[ "$(psql_smoke "SELECT count(*) FROM node_identity WHERE region = '"'"'SMOKE'"'"'")" -gt 0 ]]'
echo "    metric, node_position_history, and node_identity rows all present."

echo "==> Waiting for Grafana"
wait_until "grafana healthy" 60 "curl -sf $GRAFANA_URL/api/health >/dev/null 2>&1"

echo "==> Confirming the provisioned dashboard is reachable"
GF_PASSWORD=$(grep '^GF_SECURITY_ADMIN_PASSWORD=' tests/smoke/smoke.env | cut -d= -f2)
curl -sf -u "admin:$GF_PASSWORD" "$GRAFANA_URL/api/dashboards/uid/meshtastic-overview" | grep -q '"title":"Meshtastic Overview"'

echo "==> Smoke test passed."
