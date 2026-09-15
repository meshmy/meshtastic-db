# AGENTS.md

Living reference for this repo — facts about how the system is wired
together that aren't obvious from any single file. Read before exploring
the codebase cold. Update whenever a change affects a fact listed here;
treat a stale entry as a bug.

## What this system is

A self-hosted database that ingests Meshtastic mesh-network protobuf
packets from MQTT, BLE, USB-serial, and TCP/WiFi sources, stores telemetry
+ node position history + node identity in TimescaleDB/PostGIS, and serves
Grafana as the primary visualization layer. This file documents current,
verifiable facts about the running system — not the full design rationale
or sizing math behind each decision.

**Current status: the schema, decode library, and shared write path are all
built.** The directory layout, config templates, a two-service
`docker-compose.yml` (`timescaledb` + `grafana`), the full
TimescaleDB/PostGIS schema (`db/init/*`), `services/common/meshdb_common`'s
decode side (protobuf codegen, reflection-based decode, PortNum dispatch,
MQTT decrypt, region config loading), and its write side (`db.py` — batched
insert, dedup, as-of position join, identity upsert) all exist and are
covered by a passing test suite (unit tests plus one Docker-backed
integration suite, see below). No ingestion service or archive job exists
yet — `services/mqtt-ingest`, `services/tcp-poller`, `services/gateway-agent`,
`services/ingest-api`, `services/archive-job`, and `grafana/provisioning/*`
are still empty placeholders (`.gitkeep`). Nothing is being ingested from
the configured MQTT broker yet — `.env`/`config/regions.yaml` point at one,
but no service subscribes to it until mqtt-ingest is built.

## Where things live

- Schema: `db/init/00_extensions.sql` (timescaledb, postgis),
  `10_schema.sql` (bare tables: `metric`, `node_position_history`,
  `node_identity`, `archive_manifest`), `20_hypertable.sql`
  (`create_hypertable`, indexes, generated `geom` columns),
  `30_compression_retention.sh`, `40_continuous_aggregates.sh`
  (`metric_hourly`, `metric_daily`, `metric_daily_v`), `50_roles.sh`
  (`ingest_rw`, `grafana_ro`). The `.sh` files (30/40/50) are shell
  heredoc wrappers around `psql`, not plain `.sql` — they're the ones that
  need `.env` values (`RAW_COMPRESS_AFTER`, `HOURLY_COMPRESS_AFTER`,
  `DAILY_COMPRESS_AFTER`, `INGEST_DB_PASSWORD`, `GRAFANA_DB_PASSWORD`)
  interpolated, and `docker-entrypoint-initdb.d` is mounted read-only so a
  plain `.sql` file can't be envsubst'd in place; those five env vars are
  now passed into the `timescaledb` service in `docker-compose.yml`.
- Decode/dispatch: `services/common/meshdb_common/decode.py` —
  `walk_message()` (reflection walk, §3.3 of the design), `PORTNUM_MESSAGE_MAP`
  / `KNOWN_UNHANDLED_PORTNUMS` (the one hardcoded PortNum dispatch table,
  covered by a completeness test), `decode_data()`/`decode_service_envelope()`
  (top-level dispatch, including MQTT AES-CTR decrypt), `resolve_psk()`
  (expands a configured PSK, including the "AQ==" default-channel-key
  sentinel).
- Envelope types: `services/common/meshdb_common/envelope.py` —
  `FieldValue`, `PositionFix`, `NodeIdentityUpdate`, `DecodedPacketEnvelope`
  dataclasses; what decode.py produces and db.py consumes.
- Region config: `services/common/meshdb_common/config.py`
  (`load_regions_config()`, `resolve_secret()` for the `*_FILE` Compose-secret
  convention) and `regions.py` (`build_subscribe_topics()`,
  `is_region_allowed()`, `build_channel_psks()`).
- Shared write path: `services/common/meshdb_common/db.py` —
  `write_envelopes(conn, envelopes)` is the single entry point every
  ingestion source will call. It takes a plain `psycopg.Connection` (the
  module has no opinion on how a caller obtains one — that's each
  ingestion service's own concern) and a batch of `DecodedPacketEnvelope`,
  and in one transaction: appends `Position` envelopes to
  `node_position_history` (ON CONFLICT `(time, node_id)` DO NOTHING),
  upserts `User`/NodeInfo envelopes into `node_identity` (latest-by-`time`
  wins; a batch is first collapsed to one row per `node_id` in Python
  because Postgres rejects an `ON CONFLICT ... DO UPDATE` that would affect
  the same row twice in one statement — DO NOTHING inserts have no such
  restriction, so `metric`/positions don't need this collapse), then
  inserts every envelope's `fields` into `metric` via a `LEFT JOIN LATERAL`
  against `node_position_history` (ON CONFLICT `(time, node_id,
  metric_name, packet_id)` DO NOTHING) — positions are written first in
  the same transaction specifically so a `Position` envelope earlier in a
  batch is already visible to that join for a later envelope in the same
  batch. All three inserts use `unnest()` over parallel Python lists (one
  round trip per batch per table), not one round trip per row.
- Config: `.env.sample`, `config/regions.yaml.sample`, `secrets/*.sample` —
  copy each to its real (gitignored) filename to configure a deployment.
- Vendored protobufs: `vendor/protobufs` (git submodule, pinned commit
  below) + generated code (checked in, from `make proto-gen`) in
  `services/common/meshdb_common/generated/meshtastic/`. `import meshtastic.*`
  resolves because `meshdb_common/__init__.py` puts `generated/` on
  `sys.path` as a side effect of importing the package — protoc's own
  generated imports (`from meshtastic import mesh_pb2 as ...`) are absolute,
  not package-relative.
- Local dev/test Python environment: `services/common/pyproject.toml`
  defines the installable `meshdb-common` package (deps: `protobuf`,
  `cryptography`, `PyYAML`, `psycopg[binary]>=3.1,<4`); root
  `requirements-dev.txt` adds `pytest`, `grpcio-tools` (for `make
  proto-gen`), `ruff`, `testcontainers` (for the `db.py` integration test
  only). `ruff.toml` excludes `**/generated/` from lint (protoc output, not
  hand-written). Set up with `python3 -m venv .venv && .venv/bin/pip install
  -r requirements-dev.txt`.
- Test configuration: root `pytest.ini` registers an `integration` marker
  and sets `addopts = -m "not integration"`, so `pytest tests/`/`make test`
  runs only the fast, Docker-free unit tests by default.
  `tests/test_db_write_path.py` is the one test module marked
  `integration` — it spins up a real, disposable
  `timescale/timescaledb-ha:pg16` container running the actual
  `db/init/*` scripts (via `testcontainers`' generic `DockerContainer`,
  not the `testcontainers.postgres` module, so it can mount `db/init` at
  `/docker-entrypoint-initdb.d` exactly like `docker-compose.yml` does),
  connects as `ingest_rw` (not the superuser, so the test also exercises
  `50_roles.sh`'s actual grants), and exercises `write_envelopes()`
  end-to-end. Run it with `make test-integration`.

## Facts that must stay in sync with this file

- Vendored protobufs commit: `723a31e` (`meshtastic/protobufs`, submodule
  HEAD as of the initial scaffold — update this line whenever the
  submodule pointer moves).
- Pinned `meshtastic` PyPI version: not yet pinned (only needed once
  `gateway-agent`'s transport layer is built).
- PortNum coverage: of the 41 values currently in `portnums_pb2.PortNum`, 7
  are dispatched via `PORTNUM_MESSAGE_MAP` (Position, NodeInfo, Telemetry,
  Routing, NeighborInfo, Traceroute, MapReport) and the remaining 34 are
  deliberately unhandled via `KNOWN_UNHANDLED_PORTNUMS` — enforced by
  `tests/test_portnum_coverage.py`, which fails if a submodule bump adds a
  portnum not present in either collection.
- MQTT decrypt (`decode.decrypt_payload`/`resolve_psk`) implements AES-CTR
  with a nonce of packet_id (8 bytes LE) + from-node (4 bytes LE) + 4 zero
  bytes, and expands the "AQ==" single-byte PSK sentinel to Meshtastic's
  fixed default channel key. Both are implemented from general knowledge of
  Meshtastic's crypto scheme, self-consistently round-trip tested, but not
  yet verified against real firmware-encrypted traffic — that verification
  happens with the MQTT corpus replay test once mqtt-ingest and a live
  database exist. If real encrypted MQTT traffic decodes as
  `UNDECRYPTABLE` despite a correct configured PSK, this is the first place
  to check.
- Compose services defined so far: `timescaledb` (`timescale/timescaledb-ha:pg16`,
  host port `5432`), `grafana` (`grafana/grafana:11.3.0-ubuntu`, host port
  `3000`). No `profiles:` exist yet — `tcp-poller`/`ingest-api` and their
  `extra-sources` profile are added when those services are built.
- DB roles: `ingest_rw` (INSERT/SELECT/UPDATE on `metric`,
  `node_position_history`, `node_identity`, `archive_manifest` — no
  DELETE/TRUNCATE, confirmed by the integration test above) and
  `grafana_ro` (SELECT on `metric`, `metric_hourly`, `metric_daily`,
  `metric_daily_v`, `node_identity`, `node_position_history`), created by
  `db/init/50_roles.sh` with passwords from `INGEST_DB_PASSWORD` /
  `GRAFANA_DB_PASSWORD`. `db.py`'s `write_envelopes()` only ever needs
  `ingest_rw`'s grants (INSERT + the UPDATE that `ON CONFLICT ... DO
  UPDATE` needs for `node_identity`). Neither role is yet wired into an
  ingestion service's connection string — `db.py` takes a connection the
  caller already opened, so that wiring happens as each ingestion/
  Grafana-provisioning phase lands, not here.
- **Running `db.py`'s integration test on macOS via colima**: testcontainers'
  default cleanup ("Ryuk") bind-mounts the host Docker socket into its own
  reaper container, which fails under colima specifically (`mkdir
  .../docker.sock: operation not supported` — the socket path is on the
  macOS side, not reachable as a bind-mount source inside the colima Linux
  VM). Worked around by exporting `TESTCONTAINERS_RYUK_DISABLED=true`
  (`make test-integration` already sets it) — normal container teardown on
  the success/exit path is unaffected, since `testcontainers` still calls
  `stop()` itself; only the crash-safety-net reaper is disabled. Applies
  to any future Docker-backed test in this repo, not just this one.
- Retention/rollup values consumed by `db/init` at first container init:
  `RAW_COMPRESS_AFTER=10 days` (compression policy on raw `metric`),
  `HOURLY_COMPRESS_AFTER=30 days`, `DAILY_COMPRESS_AFTER=90 days`
  (compression policies on the two continuous aggregates). No
  `add_retention_policy()`/drop-chunks policy exists on `metric` — the
  archive job will drive `drop_chunks` explicitly instead;
  `RAW_RETENTION_INTERVAL` in `.env.sample` is reserved for that job and is
  not yet consumed by anything.
- Continuous aggregate refresh schedules (fixed, not env-configurable):
  `metric_hourly` refreshes every 30 min (`start_offset` 3h, `end_offset`
  10min); `metric_daily` (built hierarchically from `metric_hourly`, not
  raw) refreshes hourly (`start_offset` 3 days, `end_offset` 1h). Neither
  tier ever gets a retention/drop policy — both are kept forever. Of the
  three tiers, `metric_hourly` is the one to watch for unbounded growth:
  raw is capped by its 1-year hot window and `metric_daily` stays
  low-cardinality by construction, but hourly grows for as long as it's
  kept "forever" and scales linearly with node count.
- One known-benign warning on init: `30_compression_retention.sh` logs
  `WARNING: column "packet_id" should be used for segmenting or ordering`
  when compressing `metric` — expected, since `packet_id` is part of the
  primary key but deliberately excluded from `compress_segmentby`/
  `compress_orderby`: it's high-cardinality per row and doesn't help
  compression segmenting.

## How to read/connect to this data

- Read-only SQL: `grafana_ro` role covers `metric`, `metric_hourly`,
  `metric_daily`, `metric_daily_v`, `node_identity`,
  `node_position_history` (grants cascade onto hypertable chunks and
  continuous-aggregate internal views automatically).
- Spatial columns: `geom GEOGRAPHY(Point,4326)` (generated, GiST-indexed)
  on `metric` and `node_position_history` — any PostGIS-aware client
  (QGIS, `ogr2ogr`, DuckDB spatial) can connect directly; no bespoke API
  is required for read access.
- No data has been ingested yet — schema only. Verified manually (insert +
  `refresh_continuous_aggregate` + query on both `metric_hourly` and
  `metric_daily`); no persistent test data was left behind
  (`docker compose down -v timescaledb` after verification).

## Extension points (general — not tied to any one future feature)

- A new read-only consumer of this data should query the database directly
  through a dedicated read-only role, not a new bespoke API — the schema is
  the stable interface.
- A new packet-derived fact should extend the reflection-based decode path
  (`meshdb_common.decode.walk_message`), not add hardcoded per-field logic —
  touch `PORTNUM_MESSAGE_MAP` only when an entirely new top-level portnum
  needs introducing.
- A new ingestion source should call `meshdb_common.db.write_envelopes()`
  rather than writing to the database directly, so dedup/position-join/
  identity logic isn't duplicated. It only needs a `psycopg.Connection` (as
  `ingest_rw`) and a batch of `DecodedPacketEnvelope` — it has no opinion
  on transport, batching cadence, or how the connection was obtained.

## Operational notes

- Local dev setup: `cp .env.sample .env`, `cp config/regions.yaml.sample
  config/regions.yaml`, `cp secrets/*.sample` to their non-`.sample` names,
  then `docker compose up -d timescaledb grafana` (or `make up`).
- Python dev/test setup (for `meshdb_common`, independent of the above):
  `python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`,
  then `.venv/bin/pytest tests/` or `.venv/bin/ruff check services/ tests/`
  (or `make test`/`make lint` with the venv active). `make proto-gen` needs
  the same venv (`grpcio-tools`) and the `vendor/protobufs` submodule
  checked out. `make test-integration` needs Docker (colima on macOS — see
  the Ryuk/colima note above) and runs the one `db.py` integration test
  separately from `make test`.
- Retention/rollup changes need an explicit `make retune-retention` run,
  not a config reload — `docker-entrypoint-initdb.d` only runs once,
  against an empty volume, so editing `.env` after first init has no
  effect on a running deployment. The `retune-retention` Makefile target
  is still a stub that exits non-zero; the schema it would act on
  (compression policies on `metric`/`metric_hourly`/`metric_daily`) now
  exists, but the target itself is unimplemented pending a phase that
  needs it.
- `vendor/protobufs` bumps will only auto-merge once the metric-name
  stability test exists — a failure there means a human needs to look, not
  that CI is broken.
