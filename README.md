# meshtastic-telemetry-db

A self-hosted database that ingests Meshtastic mesh-network protobuf packets
from MQTT, BLE, USB-serial, and TCP/WiFi sources, retains the data
indefinitely at bounded storage cost via tiered rollups, and serves Grafana
as the primary visualization layer.

Status: schema, decode library, and all four ingestion services
(mqtt-ingest, tcp-poller, ingest-api, gateway-agent) are built. See
[AGENTS.md](AGENTS.md) for what currently exists and how the repo is laid
out.

## Quickstart (current scope)

```bash
cp .env.sample .env
cp config/regions.yaml.sample config/regions.yaml   # edit allowed_regions and MQTT_HOST
cp secrets/mqtt_password.txt.sample secrets/mqtt_password.txt
cp secrets/db_password.txt.sample secrets/db_password.txt
cp secrets/ingest_api_token.txt.sample secrets/ingest_api_token.txt

docker compose up -d
```

`timescaledb`, `grafana`, and `mqtt-ingest` all start by default.
Grafana: http://localhost:3000 (default admin password from `.env`).
Postgres/TimescaleDB: `localhost:5432`. `mqtt-ingest` subscribes to
`allowed_regions` on the broker in `config/regions.yaml` and writes decoded
packets straight to Postgres.

`tcp-poller` (polls `tcp_nodes` in `config/regions.yaml`) and `ingest-api`
(an HTTP front door for remote `gateway-agent` instances) both start with
`docker compose --profile extra-sources up -d`. `gateway-agent` itself runs
separately, on whatever host has BLE/USB access to a radio — see
`docker-compose.gateway-agent.yml` and
[AGENTS.md](AGENTS.md)'s operational notes.

The demo dashboard and the archive job land in later phases — see
[AGENTS.md](AGENTS.md) for current status.

## License

MIT — see [LICENSE](LICENSE).
