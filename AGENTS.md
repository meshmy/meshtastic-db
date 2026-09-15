# AGENTS.md

Living reference for this repo — facts about how the system is wired
together that aren't obvious from any single file. Read before exploring
the codebase cold. Update whenever a change affects a fact listed here;
treat a stale entry as a bug.

## What this system is

A self-hosted database that ingests Meshtastic mesh-network protobuf
packets from MQTT, BLE, USB-serial, and TCP/WiFi sources, stores telemetry
+ node position history + node identity in TimescaleDB/PostGIS, and serves
Grafana as the primary visualization layer. Design rationale, alternatives
considered, and sizing math live in the originating plan document, not in
this repo.

**Current status: Phase 0 (scaffold) only.** The directory layout, config
templates, and a two-service `docker-compose.yml` (`timescaledb` +
`grafana`) exist and boot. No schema, decode pipeline, or ingestion
services have been built yet — `db/init/`, `services/*/` (other than
`common/meshdb_common/generated/`), and `grafana/provisioning/*` are
currently empty placeholders (`.gitkeep`).

## Where things live

- Schema: `db/init/*.sql` — **not yet written** (Phase 1). Will hold
  `metric`, `node_position_history`, `node_identity`, `metric_hourly`,
  `metric_daily`, `archive_manifest`.
- Decode/dispatch: `services/common/meshdb_common/decode.py` — **not yet
  written** (Phase 2).
- Shared write path (dedup, position-join, identity upsert):
  `services/common/meshdb_common/db.py` — **not yet written** (Phase 3).
- Config: `.env.sample`, `config/regions.yaml.sample`, `secrets/*.sample` —
  copy each to its real (gitignored) filename to configure a deployment.
- Vendored protobufs: `vendor/protobufs` (git submodule) + generated code
  (checked in once Phase 2 runs `make proto-gen`) in
  `services/common/meshdb_common/generated/`.

## Facts that must stay in sync with this file

- Vendored protobufs commit: `723a31e` (`meshtastic/protobufs`, submodule
  HEAD as of Phase 0 — update this line whenever the submodule pointer
  moves).
- Pinned `meshtastic` PyPI version: not yet pinned (introduced in Phase 6,
  for `gateway-agent` transport only).
- Compose services defined so far: `timescaledb` (`timescale/timescaledb-ha:pg16`,
  host port `5432`), `grafana` (`grafana/grafana:11.3.0-ubuntu`, host port
  `3000`). No `profiles:` exist yet — `tcp-poller`/`ingest-api` and their
  `extra-sources` profile are added when those services are built.
- DB roles (`ingest_rw`, `grafana_ro`): not yet created — no schema exists
  (Phase 1).
- Default retention/rollup values live in `.env.sample`
  (`RAW_RETENTION_INTERVAL=1 year`, `RAW_COMPRESS_AFTER=10 days`,
  `HOURLY_COMPRESS_AFTER=30 days`, `DAILY_COMPRESS_AFTER=90 days`) but are
  inert until Phase 1's `db/init` scripts consume them.

## How to read/connect to this data

Not yet applicable — no schema or data exists. Once Phase 1 lands, the
`grafana_ro` role will be the read-only path for `metric`, `metric_hourly`,
`metric_daily`, `node_identity`, `node_position_history`, and both `metric`
and `node_position_history` will carry a `geom GEOGRAPHY(Point,4326)`
column so any PostGIS-aware client can connect directly — no bespoke API
required for read access.

## Extension points (general — not tied to any one future feature)

- A new read-only consumer of this data should query the database directly
  through a dedicated read-only role, not a new bespoke API — the schema is
  the stable interface.
- A new packet-derived fact should extend the reflection-based decode path
  (once it exists, Phase 2), not add hardcoded per-field logic — touch
  `PORTNUM_MESSAGE_MAP` only when an entirely new top-level portnum needs
  introducing.
- A new ingestion source should call into `meshdb_common.db`'s write path
  (once it exists, Phase 3) rather than writing to the database directly,
  so dedup/position-join/identity logic isn't duplicated.

## Operational notes

- Local dev setup: `cp .env.sample .env`, `cp config/regions.yaml.sample
  config/regions.yaml`, `cp secrets/*.sample` to their non-`.sample` names,
  then `docker compose up -d timescaledb grafana` (or `make up`).
- Retention/rollup changes will need an explicit `make retune-retention`
  run once Phase 1 lands, not a config reload (`docker-entrypoint-initdb.d`
  only runs once, against an empty volume). The `retune-retention` Makefile
  target currently exists only as a stub that exits non-zero, since there
  is no schema yet for it to act on.
- `vendor/protobufs` bumps will only auto-merge once the metric-name
  stability test (Phase 9) exists — a failure there means a human needs to
  look, not that CI is broken.
