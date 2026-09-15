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

**Current status: the schema, decode library, shared write path, and the
mqtt-ingest and tcp-poller services are all built.** The directory layout,
config templates, the TimescaleDB/PostGIS schema (`db/init/*`),
`services/common/meshdb_common`'s decode side (protobuf codegen,
reflection-based decode, PortNum dispatch, MQTT decrypt, region config
loading), its write side (`db.py` — batched insert, dedup, as-of position
join, identity upsert), its shared ingestion-service plumbing (`batching.py`,
`connect.py`, `stream_framing.py` — see below), `services/mqtt-ingest` (the
central MQTT subscriber), and `services/tcp-poller` (the central TCP
poller) all exist and are covered by a passing test suite (unit tests plus
Docker-backed integration suites, see below). `docker-compose.yml` now has
four services: `timescaledb`, `grafana`, `mqtt-ingest` (no `profiles:` —
starts by default with a plain `docker compose up`), `tcp-poller` (built
from `services/tcp-poller/Dockerfile`, behind `profiles: ["extra-sources"]`
— not started by a plain `docker compose up`). `services/gateway-agent`,
`services/ingest-api`, `services/archive-job`, and `grafana/provisioning/*`
are still empty placeholders (`.gitkeep`).

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
  covered by a completeness test), `decode_data()` (portnum dispatch on an
  already-decrypted `Data` payload), `decode_mesh_packet()` (decrypts a
  `MeshPacket` if needed, given a `channel_id` — or `None` when the
  transport has no such concept — and a PSK map, then calls `decode_data()`;
  shared by every transport), `decode_service_envelope()` (unwraps an MQTT
  `ServiceEnvelope` and calls `decode_mesh_packet()` with its `channel_id`),
  `resolve_psk()` (expands a configured PSK, including the "AQ=="
  default-channel-key sentinel).
- Shared ingestion-service plumbing (used by both `mqtt-ingest` and
  `tcp-poller`, factored out once a second consumer needed it):
  `services/common/meshdb_common/batching.py` (`EnvelopeBatcher` — buffers
  decoded envelopes, flushes to `write_envelopes()` on a size/time
  threshold), `services/common/meshdb_common/connect.py`
  (`build_ingest_dsn()`/`connect_with_retry()` — the `ingest_rw` connection
  bootstrap, retrying for up to 60s since container start order isn't the
  same as "ready to accept connections"), `services/common/meshdb_common/stream_framing.py`
  (`FrameReader`/`encode_frame()` — the Meshtastic local-API wire framing:
  `0x94 0xC3` magic bytes + big-endian uint16 length prefix, implemented
  directly rather than via the `meshtastic` PyPI package's `StreamInterface`,
  which would decode incoming frames through its own bundled protobuf
  classes before handing anything back — see `tcp-poller` below for why this
  matters).
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
- `services/mqtt-ingest/main.py` — the central MQTT subscriber:
  `run(config_path, ...)` loads `config/regions.yaml`, subscribes one
  paho-mqtt client per configured broker to
  `regions.build_subscribe_topic_filters()`'s filters, and on every message
  resolves which allowed region the topic matched via MQTT wildcard
  matching (`paho.mqtt.client.topic_matches_sub`, not by re-parsing
  `topic_template` against the concrete topic — ambiguous in general once
  `#`/`+` are involved), decodes via `decode_service_envelope()`, and hands
  the result to an `EnvelopeBatcher` that flushes to `write_envelopes()`
  once `MQTT_INGEST_BATCH_SIZE` (default 50) envelopes are buffered or
  `MQTT_INGEST_BATCH_INTERVAL_SECONDS` (default 2) has elapsed since the
  oldest one — whichever comes first. `run()` takes optional `conn`,
  `batch_size`, `batch_interval`, and `stop_event` overrides specifically so
  tests can drive it directly instead of only through the container
  entrypoint. DB connection retries for up to 60s
  (`connect_with_retry()`) since container start order isn't the same as
  "ready to accept connections". Credentials: `MQTT_USERNAME`/
  `MQTT_PASSWORD` (or `MQTT_PASSWORD_FILE`, the Compose-secret convention)
  and `INGEST_DB_PASSWORD`; `INGEST_DB_HOST`/`PORT`/`NAME` default to
  `timescaledb`/`5432`/`meshtastic` (the values that match
  `docker-compose.yml`).
- `services/tcp-poller/main.py` — the central TCP poller: `run(config_path,
  ...)` loads `config/regions.yaml`, spawns one thread per `tcp_nodes` entry
  (`poll_node()`), each of which connects directly to `<host>:<TCP_POLLER_PORT>`
  (default 4403, the Meshtastic local-API port), sends a framed
  `ToRadio{want_config_id: <random>}` to start the device streaming, then
  reads framed `FromRadio` messages in a loop via `meshdb_common.stream_framing`.
  Only the `packet` variant of `FromRadio.payload_variant` is handled (config/
  node-info-db/channel-dump variants sent during the initial handshake are
  ignored) — each `MeshPacket` is decoded via `decode_mesh_packet(...,
  source="tcp", channel_id=None)` (local-API traffic arrives already
  decrypted by the node itself, so `channel_id=None` only matters for the
  rare packet the node still forwards encrypted, via a configured `"*"`
  wildcard PSK) and handed to the same `EnvelopeBatcher` class `mqtt-ingest`
  uses. On any socket error (including a clean close, detected as an empty
  `recv()`), the connection thread reconnects after `TCP_POLLER_RECONNECT_DELAY_SECONDS`
  (default 5s) — other nodes' threads are unaffected. Deliberately does
  **not** depend on the `meshtastic` PyPI package: TCP framing is simple and
  fully documented (unlike BLE GATT), so implementing it directly against
  this repo's own generated `mesh_pb2.FromRadio`/`ToRadio` classes keeps the
  "meshdb_common's own regenerated classes always do the field-level decode"
  property intact without needing the interception spike `gateway-agent`
  will need for BLE/serial. Same `run()` testability hooks as `mqtt-ingest`
  (`conn`, `batch_size`, `batch_interval`, `stop_event`), plus `port` and
  `reconnect_delay` so a test can point it at a fake TCP server on an
  ephemeral port. Credentials: only `INGEST_DB_PASSWORD` — the TCP local API
  has no authentication of its own.
- `services/common/meshdb_common/regions.py` also has
  `build_subscribe_topic_filters()`, returning `(topic_filter, region)`
  pairs — `build_subscribe_topics()` is now a thin wrapper over it that
  drops the region half, kept for callers that don't need the pairing.
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
  runs only the fast, Docker-free unit tests by default. Run the
  `integration`-marked tests with `make test-integration`.
  `tests/conftest.py` holds the shared Docker-backed fixtures:
  - `ingest_dsn` (session-scoped): a real, disposable
    `timescale/timescaledb-ha:pg16` container running the actual
    `db/init/*` scripts (via `testcontainers`' generic `DockerContainer`,
    not the `testcontainers.postgres` module, so it can mount `db/init` at
    `/docker-entrypoint-initdb.d` exactly like `docker-compose.yml` does),
    exposing a DSN that connects as `ingest_rw` (not the superuser, so any
    test using it also exercises `50_roles.sh`'s actual grants). One
    container is shared across every integration test module in a run;
    tests stay isolated from each other by using disjoint `node_id`s
    rather than by resetting the database between tests (`ingest_rw` has
    no TRUNCATE/DELETE grant anyway).
  - `mosquitto_broker` (session-scoped): a real, disposable
    `eclipse-mosquitto:2` container (anonymous access enabled) for tests
    that need actual MQTT wire traffic. Its bind-mounted conf dir is
    created under the repo tree (`.mqtt-ingest-test-*/`, gitignored), not
    the system tempdir — on macOS via colima, Docker only sees paths under
    the colima VM's mounted directories (the user's home directory by
    default), and `/tmp`/`/var/folders/...` aren't among them, so a
    tempdir-based bind mount silently mounts nothing and mosquitto fails
    to find its config.
  - `tests/test_db_write_path.py` uses `ingest_dsn` to exercise
    `write_envelopes()` directly.
  - `tests/test_mqtt_ingest_replay.py` (§10.3's "MQTT corpus replay") uses
    both fixtures together: it loads `services/mqtt-ingest/main.py` via
    `importlib` (its directory has a hyphen, so it isn't a plain importable
    package), runs its `run()` in a background thread against the real
    Postgres container with a `stop_event`, publishes synthesized
    `ServiceEnvelope` bytes (plaintext and default-PSK-encrypted) to the
    real mosquitto broker on topics shaped like real Meshtastic MQTT
    traffic, and polls Postgres for the expected `metric` rows — the first
    test to exercise the actual MQTT subscribe path over real wire
    traffic, not just `decode_service_envelope()` called directly.
  - `tests/test_tcp_poller_replay.py` (the TCP analogue) uses `ingest_dsn`
    plus its own `FakeRadioServer` fixture (a plain local TCP socket, no
    container needed) that accepts one connection, drains the client's
    initial `ToRadio{want_config_id}` frame, and lets the test push
    `FromRadio{packet: MeshPacket}` frames on demand via
    `meshdb_common.stream_framing.encode_frame()`; it loads
    `services/tcp-poller/main.py` the same `importlib` way and asserts the
    expected `metric` row lands. `tests/test_stream_framing.py` covers
    `FrameReader`/`encode_frame()` directly as a pure byte-buffer state
    machine (frame split across chunks, multiple frames in one chunk,
    resync past garbage bytes, oversized-length rejection) — fast, no
    Docker needed.

## Facts that must stay in sync with this file

- Vendored protobufs commit: `723a31e` (`meshtastic/protobufs`, submodule
  HEAD as of the initial scaffold — update this line whenever the
  submodule pointer moves).
- Pinned `meshtastic` PyPI version: not yet pinned. `tcp-poller` doesn't
  need it (§ "Where things live" above) — only `gateway-agent`'s BLE/serial
  transport is expected to need it, once that phase's interception spike
  resolves how to grab raw payload bytes before the library's own bundled
  protobuf classes touch them.
- `tcp-poller` env vars (all optional, sensible defaults): `TCP_POLLER_PORT`
  (default 4403, the Meshtastic local-API TCP port), `TCP_POLLER_BATCH_SIZE`
  (default 50), `TCP_POLLER_BATCH_INTERVAL_SECONDS` (default 2),
  `TCP_POLLER_RECONNECT_DELAY_SECONDS` (default 5, applied per-node after
  any connect failure or dropped connection).
- PortNum coverage: of the 41 values currently in `portnums_pb2.PortNum`, 7
  are dispatched via `PORTNUM_MESSAGE_MAP` (Position, NodeInfo, Telemetry,
  Routing, NeighborInfo, Traceroute, MapReport) and the remaining 34 are
  deliberately unhandled via `KNOWN_UNHANDLED_PORTNUMS` — enforced by
  `tests/test_portnum_coverage.py`, which fails if a submodule bump adds a
  portnum not present in either collection.
- MQTT decrypt (`decode.decrypt_payload`/`resolve_psk`) implements AES-CTR
  with a nonce of packet_id (8 bytes LE) + from-node (4 bytes LE) + 4 zero
  bytes, and expands the "AQ==" single-byte PSK sentinel to Meshtastic's
  fixed default channel key. Verified against real firmware-encrypted
  traffic (manually, running `mqtt-ingest` against a live personal broker;
  telemetry rows landed in `metric` correctly), not just the
  self-consistent round-trip unit tests and the corpus-replay test's
  synthetic packets. If real encrypted MQTT traffic decodes as
  `UNDECRYPTABLE` despite a correct configured PSK, this is still the first
  place to check.
- Channel PSK lookup (`decode.decode_service_envelope`) supports a
  wildcard channel entry: `config/regions.yaml`'s `mqtt.channels` may
  include one named `"*"` (`decode.WILDCARD_CHANNEL`), tried whenever an
  incoming `ServiceEnvelope.channel_id` doesn't match any explicitly-named
  entry. This exists because the default PSK is a single fixed key
  independent of channel name — Meshtastic's default-modem-preset channel
  names (`LongFast`, `ShortFast`, `MediumSlow`, etc.) and any custom-named
  channel left on the default key all decrypt with it — so one `"*"` entry
  with `psk_base64: "AQ=="` (the shipped sample/local default) covers all
  of them without enumerating every preset name. An exact `channel_id`
  match always takes priority over `"*"` when both are configured, so a
  channel with a genuinely custom PSK can still be named explicitly. A
  channel using an unconfigured custom (non-default) PSK will still be
  attempted against `"*"`'s key and almost always fail to parse as a valid
  `Data` message (caught as `UNDECRYPTABLE`, same as before) — there's no
  way to distinguish "uses the default key" from "uses an unknown custom
  key" from `channel_id` alone.
- Compose services defined so far: `timescaledb` (`timescale/timescaledb-ha:pg16`,
  host port `5432`), `grafana` (`grafana/grafana:11.3.0-ubuntu`, host port
  `3000`), `mqtt-ingest` (built locally from `services/mqtt-ingest/Dockerfile`,
  no host port — outbound-only, no `profiles:` so it starts by default),
  `tcp-poller` (built locally from `services/tcp-poller/Dockerfile`, no host
  port, `profiles: ["extra-sources"]` — needs `docker compose --profile
  extra-sources up` or an explicit service name, since an empty
  `tcp_nodes: []` is the common case and there's nothing useful for it to do
  by default). `ingest-api` still needs the `extra-sources` profile added
  when it's built. `mqtt-ingest` needs `MQTT_HOST` passed through to its
  container environment (in addition to `MQTT_USERNAME`/
  `MQTT_PASSWORD_FILE`/`INGEST_DB_PASSWORD`) because `config/regions.yaml`'s
  `${MQTT_HOST}` is expanded by `load_regions_config()` against the
  *container's* environment, not the host's `.env` — `os.path.expandvars`
  silently leaves an unset reference as the literal string
  `${MQTT_HOST}` rather than erroring, which surfaces downstream as a DNS
  resolution failure on that literal string if the env var is missing from
  a service definition. Caught by actually running `docker compose up
  mqtt-ingest` against a real broker, not by the test suite (the
  corpus-replay test supplies the broker host directly, with no
  `${VAR}` indirection) — worth remembering if a future ingestion service
  reads `regions.yaml` fields that reference other `${VAR}`s.
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
- The schema itself was verified manually before any ingestion service
  existed (insert + `refresh_continuous_aggregate` + query on both
  `metric_hourly` and `metric_daily`); no persistent test data was left
  behind (`docker compose down -v timescaledb` after verification).
  `mqtt-ingest` and `tcp-poller` can now populate real data from a
  configured broker/node, but whether either has actually run against one
  depends on the deployment.

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
  edit `allowed_regions` and `MQTT_HOST`, then `docker compose up -d` (or
  `make up`) — `timescaledb`, `grafana`, and `mqtt-ingest` start by default;
  `tcp-poller` needs `docker compose --profile extra-sources up -d` (or
  naming it explicitly) plus at least one `tcp_nodes` entry in
  `config/regions.yaml` to have anything to do.
- Python dev/test setup (for `meshdb_common` and the services, independent
  of the above): `python3 -m venv .venv && .venv/bin/pip install -r
  requirements-dev.txt`, then `.venv/bin/pytest tests/` or `.venv/bin/ruff
  check services/ tests/` (or `make test`/`make lint` with the venv
  active). `make proto-gen` needs the same venv (`grpcio-tools`) and the
  `vendor/protobufs` submodule checked out. `make test-integration` needs
  Docker (colima on macOS — see the Ryuk/colima note above) and runs every
  `integration`-marked test (currently `test_db_write_path.py`,
  `test_mqtt_ingest_replay.py`, and `test_tcp_poller_replay.py`, sharing one
  Postgres container via the `ingest_dsn` fixture) separately from `make
  test`.
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
