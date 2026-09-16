# meshtastic-telemetry-db

A self-hosted database that ingests Meshtastic mesh-network protobuf packets
from MQTT, BLE, USB-serial, and TCP/WiFi sources, retains the data
indefinitely at bounded storage cost via tiered rollups, and serves Grafana
as the primary visualization layer. New upstream protobuf fields appear as
new time series automatically — no code or migration change — because
ingestion decodes via protobuf reflection instead of a fixed per-field
schema.

Status: the schema, decode library, shared write path, all four ingestion
services (mqtt-ingest, tcp-poller, ingest-api, gateway-agent), the archive
job, Grafana provisioning, and the Renovate/CI wiring are built and covered
by a passing test suite (unit tests, Docker-backed integration tests, and an
end-to-end `docker compose` smoke test). See [AGENTS.md](AGENTS.md) for a
detailed, kept-current account of what exists and how it's wired.

## Quickstart

```bash
cp .env.sample .env
cp config/regions.yaml.sample config/regions.yaml   # edit allowed_regions and MQTT_HOST — see docs/regions.md
cp secrets/mqtt_password.txt.sample secrets/mqtt_password.txt
cp secrets/db_password.txt.sample secrets/db_password.txt
cp secrets/ingest_api_token.txt.sample secrets/ingest_api_token.txt

docker compose up -d
```

`timescaledb`, `grafana`, `mqtt-ingest`, and `archive-job` all start by
default:

- **Grafana**: http://localhost:3000 (login `admin` /
  `GF_SECURITY_ADMIN_PASSWORD` from `.env`) — the `Meshtastic Overview`
  dashboard is already provisioned, with one row per Telemetry variant
  (Device/Environment/Air Quality/Power Metrics, Local Stats), a node map,
  and a per-node track/identity view.
- **Postgres/TimescaleDB**: `localhost:5432`, database `meshtastic`.
  Read-only queries (e.g. from another tool) should use the `grafana_ro`
  role rather than the superuser.
- **mqtt-ingest** subscribes to `allowed_regions` on the broker configured
  in `config/regions.yaml` and writes decoded packets straight to Postgres.
- **archive-job** exports raw data older than a year to Parquet and drops
  it from the hot table once the export is verified — the hourly/daily
  rollups it doesn't touch stay queryable forever regardless.

Two more services exist but don't start by default, since they need
additional configuration to be useful:

```bash
docker compose --profile extra-sources up -d tcp-poller ingest-api
```

- **tcp-poller** polls `tcp_nodes` entries from `config/regions.yaml` over
  the Meshtastic local API — only useful once that list is non-empty.
- **ingest-api** is an HTTP front door (`POST /v1/ingest`, bearer-token
  authenticated) for remote `gateway-agent` instances that can't reach
  Postgres directly — see [docs/gateway-agent.md](docs/gateway-agent.md).

`gateway-agent` (BLE/serial) is never part of this compose file — it runs
on whatever separate host has physical radio access, via its own
`docker-compose.gateway-agent.yml`. See
[docs/gateway-agent.md](docs/gateway-agent.md) for deployment.

## Verifying it end to end

```bash
make smoke-test
```

Builds and runs the real `docker compose` services (not a test substitute)
against a disposable MQTT broker, publishes a small synthetic packet
corpus, and confirms rows land in Postgres and the dashboard is reachable —
useful after any change to a Dockerfile, `docker-compose.yml`, or the
secrets/env wiring between them. `make test` (fast, no Docker) and `make
test-integration` (Docker-backed, drives each service's own code against
real disposable containers) cover correctness in more depth; see
[AGENTS.md](AGENTS.md) for the full test-suite breakdown.

## Operational runbooks

- [docs/regions.md](docs/regions.md) — configuring which regions/brokers/channels
  to ingest, and how to tell a region config is actually working.
- [docs/gateway-agent.md](docs/gateway-agent.md) — deploying a BLE/serial
  gateway-agent on a remote host, serial vs. BLE, WAL spillover.
- [docs/archive-and-retention.md](docs/archive-and-retention.md) — the
  raw/hourly/daily tiers, what's live-reloadable vs. init-once, running an
  archive pass manually, querying archived Parquet data.

## License

MIT — see [LICENSE](LICENSE).
