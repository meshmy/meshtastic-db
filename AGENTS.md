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

**Current status: the schema, decode library, shared write path, all four
ingestion services (mqtt-ingest, tcp-poller, ingest-api, gateway-agent),
the archive job, Grafana provisioning, the Renovate/CI wiring, an
end-to-end `docker compose` smoke test, and operator-facing runbooks
(`docs/*.md`) are all built.** The directory
layout, config templates, the TimescaleDB/PostGIS schema (`db/init/*`),
`services/common/meshdb_common`'s decode side (protobuf codegen,
reflection-based decode, PortNum dispatch, MQTT decrypt, region config
loading), its write side (`db.py` — batched insert, dedup, as-of position
join, identity upsert), its shared ingestion-service plumbing
(`batching.py`, `connect.py`, `stream_framing.py`, `serialization.py` —
see below), `services/mqtt-ingest` (the central MQTT subscriber),
`services/tcp-poller` (the central TCP poller), `services/ingest-api` (the
HTTP front door for remote gateway-agents), `services/gateway-agent` (the
BLE/serial agent for a host with physical radio access),
`services/archive-job` (scheduled chunk export to Parquet + manifest-gated
`drop_chunks`), and `grafana/` (datasource + dashboard provisioning) all
exist and are covered by a passing test suite (unit tests plus
Docker-backed integration suites, see below). `docker-compose.yml` has six
services: `timescaledb`, `grafana`, `mqtt-ingest` and `archive-job` (no
`profiles:` — start by default with a plain `docker compose up`),
`tcp-poller` and `ingest-api` (both built locally, behind `profiles:
["extra-sources"]` — not started by a plain `docker compose up`).
`docker-compose.gateway-agent.yml` is a separate, standalone compose file
for a remote host with radio access.

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
- Envelope JSON (de)serialization:
  `services/common/meshdb_common/serialization.py` —
  `envelope_to_dict()`/`envelope_from_dict()`, the wire format between
  gateway-agent (decodes locally, has no DB access) and ingest-api (writes
  to Postgres, has no decode logic of its own), and also gateway-agent's
  own WAL line format.
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
- `services/ingest-api/main.py` — thin FastAPI HTTP front door for remote
  `gateway-agent` instances that can't reach Postgres directly:
  `create_app(conn=None, cfg=None)` is a factory (tests inject an
  already-open connection/config; the container entrypoint's bare `app` at
  module level calls it with no arguments, so `uvicorn main:app` gets a
  real one via `connect_with_retry()`/`load_regions_config()` lazily, at
  `lifespan` startup, not at import time). `POST /v1/ingest` takes a JSON
  array of envelopes in `meshdb_common.serialization`'s wire format,
  requires `Authorization: Bearer <INGEST_API_TOKEN>` (checked via
  `resolve_secret`, so `INGEST_API_TOKEN_FILE` — the Compose-secret
  convention — works too), drops any envelope whose `region` isn't in
  `allowed_regions` (defense in depth against a misconfigured
  gateway-agent, the same pattern mqtt-ingest/tcp-poller apply to their own
  transports) and logs a warning per drop, then calls the same
  `write_envelopes()` every other ingestion source uses — this service has
  no decode logic of its own. Writes are serialized behind a
  `threading.Lock` around one shared `psycopg.Connection` rather than a
  connection pool: §8's own sizing (tens of rows/sec network-wide) makes a
  pool unnecessary. `GET /healthz` for container health checks.
- `services/gateway-agent/main.py` — the BLE/serial agent for a host with
  physical radio access, POSTing decoded envelopes to `ingest-api` instead
  of writing to Postgres directly. **Does not depend on the `meshtastic`
  PyPI package** — this was flagged in the design as needing a
  transport-interception spike before implementation, and the spike's
  conclusion (see the module's own docstring for the full reasoning) is
  that depending on it is actively wrong, not just unnecessary: that
  package's bundled protobuf classes live at the exact same import path
  (`meshtastic.mesh_pb2` etc.) this repo's own generated classes are put on
  via `meshdb_common/__init__.py`'s sys.path insertion, and since a regular
  package (the pip one) always wins that name over a namespace-package
  portion (this repo's `generated/meshtastic/`, which has no `__init__.py`)
  regardless of sys.path order, having both installed in one interpreter
  would silently make every decode in that process use the pip package's
  (possibly stale) schema instead of this repo's freshly-regenerated one.
  Serial reuses `meshdb_common.stream_framing`/`mesh_pb2` directly via
  `pyserial` — the same wire framing tcp-poller already proves works for
  the TCP local API, since Meshtastic's serial and TCP local APIs are
  documented to share it. BLE uses `bleak` against Meshtastic's documented
  GATT UUIDs (`BLE_FROMRADIO_UUID`/`BLE_TORADIO_UUID`/`BLE_FROMNUM_UUID`)
  directly, instead of the `meshtastic` package's `BLEInterface` — **not
  validated against real BLE hardware** (only the serial path and the
  decode/batch/HTTP-forward plumbing downstream of "raw FromRadio bytes
  off the wire" are tested; see below). Both `serial`/`bleak` imports are
  deferred into their respective transport functions rather than done at
  module load time, so the module (and everything downstream of it) stays
  importable/testable even in an environment with only one of the two
  installed. `Wal` (append-only JSONL spillover) and `IngestApiSender`
  (POSTs a batch, always replays any pending WAL backlog first and in
  order, spills the current batch to WAL on any failure including the
  backlog's own replay) give the "batches decoded envelopes and POSTs to
  ingest-api; on network failure, spills to a local append-only WAL file
  and replays on reconnect" behavior from the design. `HttpEnvelopeBatcher`
  is the HTTP-forwarding analogue of `meshdb_common.batching.EnvelopeBatcher`
  (same size/interval-threshold flush logic) — kept local to this service
  rather than factored into `meshdb_common`, since its flush target and
  failure handling (WAL spillover) differ enough from the DB-writing
  batcher that sharing would need its own seam, and only this one service
  needs it. Config: `config.example.yaml` (copy to `config.yaml` per agent
  instance) — one static `region` and one `connection` (`{type: serial,
  port: ...}` or `{type: ble, ble_address: ...}`) per agent instance, an
  `ingest_api.url`, and a `wal.path`; the bearer token itself comes from
  `INGEST_API_TOKEN`/`INGEST_API_TOKEN_FILE`, not the YAML, per the
  credentials-vs-plain-config split (§4.2). `run()` has the same
  testability-hook shape as mqtt-ingest/tcp-poller
  (`ingest_api_url`/`token`/`wal_path`/`batch_size`/`batch_interval`/
  `reconnect_delay`/`stop_event` overrides).
- `services/common/meshdb_common/regions.py` also has
  `build_subscribe_topic_filters()`, returning `(topic_filter, region)`
  pairs — `build_subscribe_topics()` is now a thin wrapper over it that
  drops the region half, kept for callers that don't need the pairing.
- `services/archive-job/export_parquet.py` — the scheduled job that drives
  `drop_chunks()` on `metric` (§6 of the design; no service writes to
  `archive_manifest` except this one). No `main.py`; the module is both the
  library and the entrypoint. Per aged chunk (`find_aged_chunks()` —
  `timescaledb_information.chunks` where `range_end <= now() -
  RAW_RETENTION_INTERVAL`), `archive_chunk()` runs export -> row-count
  verify -> sha256 -> atomic rename -> (S3 upload, if configured) ->
  `archive_manifest` insert -> `drop_chunks()` scoped to exactly that
  chunk (via `older_than`/`newer_than` bounding it to `[range_start,
  range_end)`) -> `dropped_at` update, resuming at the `drop_chunks` step
  on a re-run if a manifest row already exists without `dropped_at` (job
  died between the insert and the drop) rather than re-exporting; a chunk
  with no manifest row is simply left alone and retried next run. Export
  uses DuckDB's `postgres` scanner (`ATTACH ... (TYPE postgres)`) against
  `archive_rw`, reading the chunk table directly (`pg."<chunk_schema>"."<chunk_name>"`)
  — this also transparently reads already-compressed chunks (verified: a
  chunk compressed via the compression policy still reads correctly
  through the scanner, no special-casing needed). `metric.geom` arrives
  through the scanner as a VARCHAR of hex-EWKB (Postgres's own text output
  for a geography value with no native DuckDB equivalent);
  `ST_GeomFromHEXEWKB` (the `spatial` extension) converts it to a DuckDB
  GEOMETRY before the Parquet write, which is what makes the output
  standard GeoParquet (§2.7) rather than an opaque blob column. `duckdb`,
  `spatial`, and `postgres` extensions are `INSTALL`/`LOAD`ed at runtime
  (not baked into the image), so the container needs outbound network
  access on first run per extension version. `run_once(pg_conn, duckdb_con,
  ...)` is the core, directly testable entry point; `python
  export_parquet.py` runs it on an APScheduler cron trigger
  (`ARCHIVE_SCHEDULE_CRON`, default `0 2 * * *` UTC) via `BlockingScheduler`,
  and `python export_parquet.py --once` runs it a single time immediately
  (for manual/operational use — e.g. right after lowering
  `RAW_RETENTION_INTERVAL` to test the pipeline). S3/MinIO backend
  (`ARCHIVE_BACKEND=s3`) uploads the verified local file via `boto3` and
  then deletes the local copy — local and S3 are alternative backends, not
  additive storage.
- `grafana/` — datasource + dashboard provisioning (§7/§8 of the design;
  no application code, just config files mounted read-only into the
  `grafana` container). `grafana.ini` sets `[plugins]
  allow_loading_unsigned_plugins = motherduck-duckdb-datasource` (the one
  setting that has to live in the ini file rather than a `GF_*` env var —
  see below for why). `provisioning/datasources/datasources.yaml`
  provisions two datasources: `timescaledb` (uid), Grafana's built-in
  Postgres plugin (`type: postgres` in the YAML; the API reports it back
  as the canonical `grafana-postgresql-datasource`), connecting as
  `grafana_ro` to `timescaledb:5432` with `timescaledb: true` (enables
  Timescale-aware query-editor macros) — its password comes from
  `secureJsonData.password: ${GRAFANA_DB_PASSWORD}`, Grafana's own native
  `${VAR}`-expansion-from-container-env for provisioning files (unrelated
  to `docker-compose.yml`'s own `${VAR}` substitution; both have to line
  up, which is why `GRAFANA_DB_PASSWORD` is now also passed into the
  `grafana` service's container environment, not just `timescaledb`'s);
  and `archive-duckdb` (uid), the unsigned
  `motherduckdb/grafana-duckdb-datasource` plugin (plugin id
  `motherduck-duckdb-datasource`) with an empty `jsonData.path` (opens an
  in-memory DuckDB — no persistent `.duckdb` file) and `initSql: 'INSTALL
  spatial; LOAD spatial;'` so ad hoc queries can read `metric.geom` back
  out of an archived GeoParquet file. Every ad hoc query against it names
  its own `read_parquet('/archive/parquet/metric/...')` glob — the archive
  layout `export_parquet.py` writes (see above) — rather than the
  datasource pointing at one fixed file. `provisioning/dashboards/
  dashboards.yaml` is the file-provider config (points at
  `/etc/grafana/provisioning/dashboards`); `meshtastic-overview.json` is
  the one provisioned dashboard (uid `meshtastic-overview`, currently
  `version: 6` inside the JSON — bump this whenever the file changes, it's
  how Grafana's own file-provisioner decides a re-read copy is newer than
  what's in its database): `$region`/`$node_id` template variables
  (query-type, the second filtered by the first). **`$node_id` has a
  wildcard "All" option** (`includeAll: true`, `allValue: "-1"`, `multi:
  false`) — `-1` because `node_id` is always a non-negative uint32 off the
  wire, so no real row can ever match it, and every telemetry panel's WHERE
  clause is `($node_id::bigint = -1 OR node_id = $node_id::bigint)`: true
  unconditionally when "All" is selected (Grafana substitutes the same
  resolved value, `-1`, into both occurrences of `$node_id`), otherwise an
  exact match on whichever real node_id is picked — this is how a
  fleet-wide comparison and a single-node view are the same panel, not
  two. **Every `$node_id` reference is cast `::bigint`, in every panel that
  uses it, not just the telemetry rows** — see "Facts that must stay in
  sync" below for why this is load-bearing, not decorative.

  **One collapsed row per Telemetry oneof variant** the design calls out by
  name — Device/Environment/Air Quality/Power Metrics, Local Stats (§7's
  own explicit list; newer upstream variants — `health_metrics`,
  `host_metrics`, `soil_water_metrics`, present in `telemetry.proto` but
  not in the design doc — have no row yet, the documented
  "visualization-layer limit, not a database one"). Each row has exactly
  one *template* panel in the JSON, nested inside the row panel's own
  `panels` array (Grafana's schema for a `collapsed: true` row; its
  children are *not* top-level siblings in the dashboard's own `panels`
  list the way an expanded row's are, which is why
  `tests/test_grafana_provisioning.py`'s `_iter_panels()` has to flatten
  one level into any row before indexing panels by title) — but that one
  panel **repeats** (`"repeat": "<variant>_field"`, `"repeatDirection":
  "v"`) over a same-named, hidden (`hide: 2`), multi-value, `$region`-scoped
  variable whose query is `SELECT DISTINCT metric_name FROM metric WHERE
  packet_type = 'Telemetry' AND metric_name LIKE '<variant>.%' AND region =
  '$region'`, `current` pinned to Grafana's `$__all` sentinel (a hidden
  variable has no UI for a person to ever pick "All" themselves, so its
  default has to already resolve to every option). **This is what makes
  "one graph per metric" require zero hardcoded field list and zero
  dashboard-JSON change when a new field starts showing up**: the variable
  literally is "every `metric_name` this variant currently has in the
  database", Grafana renders one repeated panel per value, and each
  repeated instance's own query filters `metric_name = '<the one
  resolved value>'` and substitutes `$node_id` the same wildcard-aware way
  every other panel does — series labelled just
  `coalesce(short_name, long_name, '0x'||hex(node_id))` via a `LEFT JOIN
  node_identity`, since the panel's own (repeated) title already names the
  field. A brand-new protobuf field flows all the way from `vendor/protobufs`
  to its own graph on this dashboard with **no code change and no dashboard
  edit** — the same reflection property `walk_message()` gives the decode
  pipeline (§3.3), now extended one layer further into the visualization
  layer. `custom.spanNulls: true` + `showPoints: "always"` in each panel's
  `fieldConfig.defaults` connects points with a line across the gaps
  irregular telemetry intervals would otherwise leave, rather than
  Grafana's default of breaking the line at each gap.

  Two panels the wildcard doesn't reach — `$node_id track (selected time
  range)` (Geomap) and `$node_id identity` (table), both still filtered by
  `node_id = $node_id` with no `OR -1` — are deliberately left showing
  nothing when "All" is selected: a multi-node "track" would just be
  disconnected points from different nodes drawn as one nonsensical line,
  and there's no fleet-wide identity table on this dashboard any more (an
  earlier revision had one — an "All Nodes" row — removed once the
  `$node_id` wildcard made it redundant; the `$node_id` dropdown itself
  already lists every node by name to pick from). After the five collapsed
  rows: a Node Map row (two Geomap panels: current positions in `$region`,
  and the single-node track above) and a Node Detail row (the single-node
  identity table above) — both expanded by default. **Deliberately queries
  raw `metric` (time-filtered via `$__timeFilter`), not
  `metric_hourly`/`metric_daily_v`**: the continuous aggregates only
  populate on their own refresh schedule (30 min / hourly, see "Facts that
  must stay in sync" below), so a dashboard built against them would show
  empty panels for a while after fresh data lands — raw `metric` is always
  current. Switching (some or all of) these panels to the rollups for
  longer time ranges is a possible future enhancement, not wired up.

  **Every `metric_name` reference matches `decode_data()`'s actual output,
  not `db/init/10_schema.sql`'s doc comment** — that comment's example
  (`'telemetry.environment_metrics.temperature'`) has a `telemetry.` prefix
  that doesn't actually exist: `decode_data()` calls `walk_message()`
  directly on the inner Telemetry message with no prefix, so real rows are
  `environment_metrics.temperature` etc. `tests/test_grafana_provisioning.py`
  decodes real `Telemetry` messages through `walk_message()` and checks
  each variant's field-list variable query and repeat wiring
  (`test_telemetry_panel_repeats_over_its_field_variable`), plus that every
  field variable is genuinely hidden/multi-value/`$__all`-defaulted
  (`test_field_variable_is_hidden_multi_value_and_region_scoped`) —
  specifically to catch this class of mistake landing in the dashboard
  again. `integration`-marked tests run the actual field-list variable
  query through Grafana's real API and confirm it returns exactly the
  `metric_name`s just seeded (proof the "no hardcoded list" property holds
  for real, not just that the query text looks right), and run one variant
  panel's query with `$node_id` substituted `-1` vs. a real node_id —
  once, both seeded nodes' series must come back; the other, only one's —
  proof the wildcard actually switches between "all nodes" and "one node"
  rather than just widening what "one node" already showed.
- Config: `.env.sample`, `config/regions.yaml.sample`, `secrets/*.sample` —
  copy each to its real (gitignored) filename to configure a deployment.
  `secrets/ingest_api_token.txt.sample`/`INGEST_API_TOKEN` in `.env.sample`
  are now actually consumed: by `docker-compose.yml`'s `ingest-api` service
  (as `INGEST_API_TOKEN_FILE`) and by `docker-compose.gateway-agent.yml`'s
  `gateway-agent` service the same way — both sides of the same bearer
  token need to be copied from the same value for a real deployment.
  `services/gateway-agent/config.example.yaml` is the per-agent-instance
  config (region, connection, ingest-api URL, WAL path) — copied to a
  plain `config.yaml` alongside wherever `docker-compose.gateway-agent.yml`
  runs, not part of `config/regions.yaml`.
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
  proto-gen`), `ruff`, `testcontainers` (for the `db.py` integration test),
  `paho-mqtt` (mqtt-ingest), `fastapi`/`httpx` (ingest-api's
  `TestClient`), `requests`/`pyserial`/`bleak` (gateway-agent), and
  `duckdb`/`apscheduler`/`boto3` (archive-job) so every service's
  `main.py`/`export_parquet.py` is importable from the test venv, not just
  its own container image. `ruff.toml` excludes `**/generated/` from lint (protoc
  output, not hand-written) and sets `flake8-bugbear.extend-immutable-calls
  = ["fastapi.Depends"]` (FastAPI's own required idiom is a function call
  as an argument default, which bugbear's B008 would otherwise flag as the
  mutable-default-argument bug). Set up with `python3 -m venv .venv &&
  .venv/bin/pip install -r requirements-dev.txt`.
- Test configuration: root `pytest.ini` registers an `integration` marker
  and sets `addopts = -m "not integration"`, so `pytest tests/`/`make test`
  runs only the fast, Docker-free unit tests by default. Run the
  `integration`-marked tests with `make test-integration`.
  `tests/conftest.py` holds the shared Docker-backed fixtures:
  - `_timescaledb_host_port` (session-scoped): a real, disposable
    `timescale/timescaledb-ha:pg16` container running the actual
    `db/init/*` scripts (via `testcontainers`' generic `DockerContainer`,
    not the `testcontainers.postgres` module, so it can mount `db/init` at
    `/docker-entrypoint-initdb.d` exactly like `docker-compose.yml` does).
    One container is shared across every integration test module in a
    run — `ingest_dsn` and `archive_dsn` (below) both build their role DSN
    against it rather than each starting their own container.
  - `ingest_dsn` / `archive_dsn` (session-scoped, built on
    `_timescaledb_host_port`): DSNs that connect as `ingest_rw` /
    `archive_rw` respectively (never the superuser), so any test using
    either also exercises `50_roles.sh`'s actual grants for that role, not
    just the schema SQL. Tests stay isolated from each other by using
    disjoint `node_id`s (and, for archive-job, disjoint historical dates)
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
  - `tests/test_serialization.py` round-trips `DecodedPacketEnvelope`
    through `envelope_to_dict()`/`envelope_from_dict()` for all four
    envelope shapes (fields, position, identity, UNDECRYPTABLE) — fast, no
    Docker needed.
  - `tests/test_ingest_api.py` loads `services/ingest-api/main.py` the same
    `importlib` way and uses FastAPI's `TestClient` against
    `create_app(conn=..., cfg=...)` for auth/region-filtering/malformed-body
    cases with a fake connection (fast, no Docker); one `integration`-marked
    case uses the real `ingest_dsn` fixture to prove the whole HTTP ->
    `write_envelopes()` -> Postgres path, not just that the pieces are wired
    together.
  - `tests/test_gateway_agent_wal.py`, `tests/test_gateway_agent_decode.py`,
    and `tests/test_gateway_agent_http_sender.py` load
    `services/gateway-agent/main.py` the same `importlib` way — with one
    addition the other services' main.py files don't need:
    `sys.modules[spec.name] = module` before `exec_module()`, because this
    module (unlike mqtt-ingest's/tcp-poller's) defines its own `@dataclass`
    classes, and Python's dataclass processing looks the defining module up
    in `sys.modules` by name — without that registration step it raises
    `AttributeError` on a `NoneType`. None of these three need Docker:
    `test_gateway_agent_wal.py` exercises `Wal` append/replay/clear/
    torn-line-skip directly; `test_gateway_agent_decode.py` exercises
    `handle_from_radio_bytes()`/`HttpEnvelopeBatcher` against synthetic
    `FromRadio` bytes with a fake sender, proving the decode/batch wiring
    without a real transport; `test_gateway_agent_http_sender.py` runs
    `IngestApiSender` against a real local `http.server` (not a mock of the
    `requests` library) to prove both the plain forwarding path and — the
    phase's own stated acceptance criterion — that WAL spillover survives
    an actual ingest-api restart mid-stream (the test really does shut one
    fake server down and bind a second one to the same port, not just
    toggle a failure flag). The transport layer itself (serial/BLE
    connection handling) has no test — matching the design's own §10.7
    hedge that it isn't fully mockable in CI; BLE in particular has not
    been run against real hardware at all (see `services/gateway-agent/
    main.py`'s module docstring).
  - `tests/test_archive_job.py` loads `services/archive-job/export_parquet.py`
    the same `importlib` way (needing the same `sys.modules[spec.name] =
    module` pre-registration as gateway-agent's tests, since it also
    defines its own `@dataclass` classes). Uses both `ingest_dsn` (to
    write historical rows via `write_envelopes()`) and `archive_dsn` (to
    run `run_once()` as `archive_rw`, and a `duckdb_con` fixture built via
    `build_duckdb_connection(archive_dsn)`), against a `tmp_path` archive
    root. Identifies "its" chunk by date rather than by an exact global
    chunk count, since the container is shared with every other
    integration test module and some of those also insert historical
    (already-aged) data. Covers: a full export -> verify -> manifest ->
    drop_chunks run (asserting the Parquet file exists, the manifest row's
    `row_count`/`sha256`/`dropped_at` are correct, the chunk is gone from
    `timescaledb_information.chunks`, and DuckDB can read the archived
    file's `geom` column back as valid WKT); and that a second `run_once()`
    against an already-fully-archived chunk is a no-op (doesn't re-export
    or touch the existing manifest row) — the resume-at-drop_chunks path
    itself (manifest row present, `dropped_at` still null) has no test,
    since triggering it needs killing the process mid-run.
  - `tests/test_grafana_provisioning.py` covers `grafana/` in two layers.
    Fast, Docker-free tests (no `integration` mark): parse
    `datasources.yaml`/`dashboards.yaml`/`meshtastic-overview.json` and
    assert their structure, and — the regression guard described above —
    decode a real `telemetry_pb2.Telemetry` per oneof variant through
    `walk_message()` and assert each panel's `metric_name LIKE
    '<variant>.%'` filter matches what actually comes out. Three
    `integration`-marked tests bring up a real, disposable
    `grafana/grafana:11.3.0-ubuntu` container (the `grafana_base_url`
    fixture) running this repo's actual `grafana.ini`/`provisioning/`
    files unmodified, joined to a new `_test_network` (a
    `testcontainers.core.network.Network`) alongside the shared
    `_timescaledb_host_port` container — aliased `timescaledb` on that
    network, the same hostname `docker-compose.yml` gives it in
    production, so the provisioning YAML needs no test-only override —
    and drive Grafana's real HTTP API (`/api/datasources`,
    `/api/dashboards/uid/...`, `/api/ds/query`, Basic auth as `admin`)
    against it: one confirms both datasources and the dashboard are
    actually provisioned; one seeds a `metric` row via `ingest_dsn` and
    runs the Device Metrics panel's own query through `/api/ds/query`
    against the `timescaledb` datasource, asserting the seeded value comes
    back (the phase's "dashboard renders against seeded test data"
    acceptance criterion); one runs a real `export_parquet.run_once()`
    (same pattern as `test_archive_job.py`) into a bind-mounted
    `archive_root` (created under the repo tree, not the system tempdir —
    same colima-bind-mount-visibility reason as `mosquitto_broker`'s conf
    dir) and queries the `archive-duckdb` datasource's `read_parquet(...)`
    over it (the "DuckDB datasource queries an archived Parquet file"
    criterion). The DuckDB plugin's `/api/ds/query` `format` field must be
    the numeric `sqlutil.FormatQueryOption` (`1` for table), not the
    string `"table"` the built-in Postgres plugin accepts — found by
    running the test against a real container, not from any plugin doc;
    worth knowing before adding another DuckDB-datasource query
    elsewhere. `GF_INSTALL_PLUGINS` (both here and in `docker-compose.yml`)
    needs real outbound network access on every container start, same
    caveat as `services/archive-job`'s DuckDB extensions.

- `bin/smoke-test.sh` (`make smoke-test`) — the one check that runs the
  *actual* `docker-compose.yml` services (built Dockerfiles, real
  env/secrets/volume wiring), rather than driving each service's Python
  entrypoint in-process or spinning up ad hoc containers via
  `testcontainers` the way every test above does. Uses
  `docker-compose.test.yml` (an overlay — always passed together with
  `docker-compose.yml`, never standalone) to add a disposable
  anonymous-access `mosquitto` broker and repoint `mqtt-ingest` at
  `tests/smoke/regions.yaml` (region `SMOKE`) instead of a deployer's real
  `config/regions.yaml`; runs under compose project name
  `meshtastic-db-smoke` and host ports 15432/13000 (via the compose-spec
  `!override` YAML merge tag — a plain re-declaration of `ports:` in an
  overlay *appends* rather than replaces, which would otherwise double-bind
  5432/3000 alongside a real deployment's own) so it can run alongside a
  real stack without a port clash. Sequence: build `mqtt-ingest`, start
  `timescaledb`/`grafana`/`mosquitto`/`mqtt-ingest`, wait for the
  `connected, subscribed` log line (publishing before mqtt-ingest has
  actually subscribed silently loses the message — no persistent MQTT
  session is configured), run `tests/smoke/corpus_publisher.py` inside the
  already-built `mqtt-ingest` image (`docker compose run --rm --no-deps`,
  with `./tests` bind-mounted in) to publish one Telemetry/Position/NodeInfo
  packet each over real MQTT, poll Postgres for the resulting `metric`/
  `node_position_history`/`node_identity` rows, then confirm Grafana's
  `/api/health` and the provisioned dashboard's API endpoint respond.
  `secrets/mqtt_password.txt` must already exist (content unused —
  mosquitto allows anonymous access — but `docker-compose.yml` declares it
  as a required Compose secret file for the `mqtt-ingest` service); the
  script errors out early with instructions if it's missing rather than
  creating it. Always tears down (`down -v --remove-orphans`) on exit via a
  trap, success or failure.

- `docs/regions.md`, `docs/gateway-agent.md`, `docs/archive-and-retention.md`
  — operator-facing runbooks linked from `README.md`, each covering one
  area in more depth than the README's quickstart: region/broker/PSK
  configuration and how to tell it's actually working; gateway-agent
  deployment (serial vs. BLE, WAL spillover, the `meshtastic`-PyPI-package
  non-dependency); and the raw/hourly/daily tiers, which retention knobs
  are live-reloadable vs. init-once, and querying the archive. Distinct
  from this file: `AGENTS.md` is the fast-orientation reference for working
  *on* the codebase; `docs/*.md` are for someone *operating* a deployment
  and not necessarily reading source.

- `renovate.json` + `.github/workflows/` — the automated-update gating that
  keeps "always-fresh protobufs" (§3.1/§8 of the design) true over time.
  `tests/golden_metrics.py` builds a fixed corpus of synthetic messages
  (one populated instance per Telemetry oneof variant, Routing's two oneof
  arms, NeighborInfo, Traceroute, MapReport — every leaf field on each, not
  just a couple) and decodes it through `decode_data()`, producing the
  `(portnum, metric_name, value_type)` tuples that `write_golden()` freezes
  into `tests/fixtures/known_metrics_golden.json`; `make
  update-golden-metrics` reruns it, `test_metric_name_stability.py` asserts
  the golden set is still a **subset** of what current code produces (a
  pure addition — a new field — passes automatically; a rename or removal
  fails, which is the silent-fork case this exists to catch — see §8/§10.2a
  of the design for the full rationale). Deliberately scoped to the portnums
  `decode_data()` routes through `walk_message()` — Position/NodeInfo are
  excluded, since `_extract_position`/`_extract_identity` reference field
  names directly and so already fail loudly (`AttributeError`) on a rename
  rather than silently renaming a `metric_name`. `.github/workflows/ci.yml`
  runs four jobs: `make lint`, `make test`, `make test-integration` (the
  last needs no colima workaround on GitHub-hosted Ubuntu runners — Docker
  is native there; `TESTCONTAINERS_RYUK_DISABLED=true` is harmless when set
  anyway), and `make smoke-test` (copies `secrets/mqtt_password.txt.sample`
  into place first, since a fresh checkout has no real secrets — see below
  for what the smoke test itself does). `.github/workflows/proto-regen-check.yml` re-runs `make proto-gen` on any
  PR touching `vendor/protobufs`/`services/common/**` and fails on a diff.
  `.github/workflows/auto-approve-renovate.yml` approves only a PR opened
  by `renovate[bot]` carrying the `protobuf-bump` label (gated on actor +
  label alone, both GitHub-verified and not spoofable via PR content;
  never checks out the PR branch). `.github/workflows/renovate.yml` runs
  Renovate itself self-hosted (needed so `postUpgradeTasks` can execute
  `make proto-gen`/`make update-golden-metrics` against a real checkout),
  gated by `RENOVATE_ALLOWED_POST_UPGRADE_COMMANDS` regexes matching
  exactly the commands `renovate.json`'s `postUpgradeTasks` runs.

## Facts that must stay in sync with this file

- Vendored protobufs commit: `723a31e` (`meshtastic/protobufs`, submodule
  HEAD as of the initial scaffold — update this line whenever the
  submodule pointer moves).
- The `meshtastic` PyPI package is not, and will not be, a dependency of
  any service in this repo. `tcp-poller` never needed it: TCP framing is
  simple and fully documented. `gateway-agent` was expected to need it for
  BLE/serial (per the design's original plan), but its own
  transport-interception spike concluded the opposite — see
  `services/gateway-agent/main.py`'s module docstring for the full
  reasoning (short version: that package's bundled protobuf classes would
  silently shadow this repo's own generated ones via a Python import-path
  collision). If a future change ever considers adding it as a dependency
  of any service, re-read that docstring first.
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
  `3000`; mounts `./grafana/grafana.ini`, `./grafana/provisioning`, and
  `./archive:/archive:ro` — the last one so its DuckDB datasource can read
  archived Parquet files at the same `/archive/...` path
  `export_parquet.py` writes them at; env `GF_INSTALL_PLUGINS` installs the
  unsigned `motherduck-duckdb-datasource` plugin straight from its GitHub
  release zip, pinned to `v0.4.5`, on every container start — see
  "Where things live" above for the full provisioning breakdown),
  `mqtt-ingest` (built locally from `services/mqtt-ingest/Dockerfile`,
  no host port — outbound-only, no `profiles:` so it starts by default),
  `tcp-poller` (built locally from `services/tcp-poller/Dockerfile`, no host
  port, `profiles: ["extra-sources"]` — needs `docker compose --profile
  extra-sources up` or an explicit service name, since an empty
  `tcp_nodes: []` is the common case and there's nothing useful for it to do
  by default), `ingest-api` (built locally from
  `services/ingest-api/Dockerfile`, host port `8000`, also `profiles:
  ["extra-sources"]` — only needed once a remote gateway-agent exists),
  `archive-job` (built locally from `services/archive-job/Dockerfile`, no
  host port, no `profiles:` so it starts by default — bind-mounts
  `./archive:/archive`, matching `ARCHIVE_LOCAL_PATH`'s default).
  `docker-compose.gateway-agent.yml` is a separate standalone compose file
  (not merged into the main one) for a remote host with radio access —
  builds the same `services/gateway-agent/Dockerfile` against this same
  repo checkout, so deploying it means cloning this repo onto that host
  too, not just copying one file. `mqtt-ingest` needs `MQTT_HOST` passed
  through to its container environment (in addition to `MQTT_USERNAME`/
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
  DELETE/TRUNCATE, confirmed by the integration test above), `grafana_ro`
  (SELECT on `metric`, `metric_hourly`, `metric_daily`, `metric_daily_v`,
  `node_identity`, `node_position_history`), and `archive_rw` (SELECT on
  `metric`; SELECT/INSERT/UPDATE on `archive_manifest`; **also a member of
  the bootstrap superuser role** — `GRANT "${POSTGRES_USER}" TO
  archive_rw`, i.e. "postgres" by default — because `drop_chunks()`
  requires hypertable-owner privileges and a plain grant on the table
  isn't sufficient; role membership gives archive_rw owner-equivalent
  rights on `metric` without transferring the hypertable's actual
  ownership away from postgres. This is deliberately not extended to
  `ingest_rw`, to keep the elevated privilege scoped to the one
  container — archive-job — that isn't network-facing; verified against a
  real container: `GRANT postgres TO archive_rw` is sufficient for
  `archive_rw` to call `drop_chunks('metric', ...)`, including on an
  already-compressed chunk), created by `db/init/50_roles.sh` with
  passwords from `INGEST_DB_PASSWORD` / `GRAFANA_DB_PASSWORD` /
  `ARCHIVE_DB_PASSWORD`. `db.py`'s `write_envelopes()` only ever needs
  `ingest_rw`'s grants (INSERT + the UPDATE that `ON CONFLICT ... DO
  UPDATE` needs for `node_identity`). `ingest-api` is the only service that
  connects as `ingest_rw` on behalf of another process (gateway-agent
  itself has no DB access at all) rather than for its own directly-decoded
  traffic. `meshdb_common.connect` has `build_ingest_dsn()`/
  `build_archive_dsn()` for the two respective roles (same
  `INGEST_DB_HOST`/`PORT`/`NAME`, different role/password); `grafana_ro`
  has no equivalent in `meshdb_common.connect` since nothing in this repo's
  own Python code connects as it — its DSN is assembled entirely inside
  Grafana's own provisioning YAML (`grafana/provisioning/datasources/
  datasources.yaml`: `url: timescaledb:5432`, `user: grafana_ro`,
  `secureJsonData.password: ${GRAFANA_DB_PASSWORD}`, that last one
  expanded by Grafana itself from its container environment, not by any
  code in this repo).
- **Every `$node_id` reference in `meshtastic-overview.json` must be cast
  `::bigint`** (`$node_id::bigint`), never bare — confirmed live, not just
  suspected: a real node, `2769232366` ("Bukit Cermin Selangor MY_919",
  above `2**31 - 1`), broke every panel that referenced it with `ERROR:
  value "2769232366" is out of range for type integer` from Postgres
  itself. `node_id` is a full uint32 off the wire (up to ~4.29 billion,
  `metric`/`node_identity` both store it as `BIGINT`), but Grafana's
  Postgres datasource binds a bare `$node_id` template variable as an
  `integer` (int4, max ~2.15 billion) query parameter — reproduced
  directly against a live connection by binding the same value as
  `::int4` (`psycopg`'s `cur.execute("SELECT %s::int4", (2769232366,))`
  raises the identical error); binding it `::bigint` instead resolves it,
  which is what the explicit cast in the SQL text forces Postgres to
  negotiate regardless of how Grafana's own binding works internally.
  Every node whose id happened to fall in the upper half of uint32 range
  was affected, on every panel using `$node_id` — not something specific
  to one node's data, despite how it first presented ("this one node
  breaks all the graphs"). `tests/test_grafana_provisioning.py`'s
  `test_every_node_id_reference_is_cast_to_bigint` regex-scans every
  panel's `rawSql` for a bare `$node_id` not immediately followed by
  `::bigint`, and an `integration`-marked test seeds that exact node_id
  and runs the real panel query through Grafana's API to confirm it still
  works — a future panel added to this dashboard that filters on
  `$node_id` needs the same cast, or this class of bug reappears silently
  (small node_ids "work" throughout development and testing until a real
  one exceeds int32).
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
  `add_retention_policy()`/drop-chunks policy exists on `metric` — instead
  `services/archive-job/export_parquet.py` reads `RAW_RETENTION_INTERVAL`
  (default `1 year`) at each scheduled run and drives `drop_chunks`
  explicitly, only after a verified Parquet export (see "Where things
  live" above). Unlike the other four retention/rollup values above,
  `RAW_RETENTION_INTERVAL` is read live on every run, not just at first
  `db/init` — changing it in `.env` and restarting `archive-job` takes
  effect on the next scheduled tick, with no `retune-retention`-style
  caveat.
- Continuous aggregate refresh schedules (fixed, not env-configurable):
  `metric_hourly` refreshes every 30 min (`start_offset` 3h, `end_offset`
  10min); `metric_daily` (built hierarchically from `metric_hourly`, not
  raw) refreshes hourly (`start_offset` 3 days, `end_offset` 1h). Neither
  tier ever gets a retention/drop policy — both are kept forever. Of the
  three tiers, `metric_hourly` is the one to watch for unbounded growth:
  raw is capped by its 1-year hot window and `metric_daily` stays
  low-cardinality by construction, but hourly grows for as long as it's
  kept "forever" and scales linearly with node count.
- Pinned GitHub Actions in `.github/workflows/`: `actions/checkout@v7`,
  `actions/setup-python@v7`, `renovatebot/github-action@v46.1.16`,
  `hmarr/auto-approve-action@v4.0.0` — Renovate's own `github-actions`
  manager (enabled in `renovate.json`) keeps these current going forward,
  the same automated-update pattern applied to itself.
- `tests/golden_metrics.py`'s corpus currently produces 176
  `(portnum, metric_name, value_type)` tuples across 5 portnums (Telemetry,
  Routing, NeighborInfo, Traceroute, MapReport) — grows whenever a
  deliberate field is added to the corpus, shrinks never (a shrink would
  mean the corpus itself lost coverage, worth a second look).
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
  continuous-aggregate internal views automatically). Grafana's own
  provisioned `timescaledb` datasource connects as this role — see "Where
  things live" above.
- Archived Parquet files (any date range past `RAW_RETENTION_INTERVAL`,
  already dropped from the hot table): query directly via DuckDB's
  `read_parquet()` against `<ARCHIVE_LOCAL_PATH>/parquet/metric/dt=.../*.parquet`,
  or through Grafana's provisioned `archive-duckdb` datasource, no running
  database involved either way.
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
  `config/regions.yaml` to have anything to do; `ingest-api` needs the same
  `--profile extra-sources` (or naming it explicitly) and is only useful
  once a remote `gateway-agent` exists to POST to it. A `gateway-agent`
  instance runs separately, on whatever host has the physical radio: `cp
  services/gateway-agent/config.example.yaml config.yaml` (edit `region`,
  `connection`, `ingest_api.url`), `cp secrets/ingest_api_token.txt.sample
  secrets/ingest_api_token.txt` (matching the value the central
  `ingest-api` instance was actually started with), then `docker compose -f
  docker-compose.gateway-agent.yml up -d` from a checkout of this same repo
  on that host. Once up, Grafana is at `http://localhost:3000` (login
  `admin` / `GF_SECURITY_ADMIN_PASSWORD`), with the `Meshtastic Overview`
  dashboard already provisioned.
- Python dev/test setup (for `meshdb_common` and the services, independent
  of the above): `python3 -m venv .venv && .venv/bin/pip install -r
  requirements-dev.txt`, then `.venv/bin/pytest tests/` or `.venv/bin/ruff
  check services/ tests/` (or `make test`/`make lint` with the venv
  active). `make proto-gen` needs the same venv (`grpcio-tools`) and the
  `vendor/protobufs` submodule checked out. `make test-integration` needs
  Docker (colima on macOS — see the Ryuk/colima note above) and runs every
  `integration`-marked test (currently `test_db_write_path.py`,
  `test_mqtt_ingest_replay.py`, `test_tcp_poller_replay.py`,
  `test_archive_job.py`, `test_grafana_provisioning.py`, and one case in
  `test_ingest_api.py`, all sharing one Postgres container via
  `_timescaledb_host_port`/`ingest_dsn`/`archive_dsn`) separately from
  `make test`. `services/archive-job` also needs outbound network access
  the first time it runs (`INSTALL postgres`/`INSTALL spatial` fetch
  DuckDB extensions at runtime, not at container build time); the
  `grafana` container needs it on every start (`GF_INSTALL_PLUGINS` fetches
  the unsigned DuckDB datasource plugin's zip fresh each time, not just
  once) — a fully offline environment would need both pre-cached.
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
- Manually triggering an archive run (outside its `ARCHIVE_SCHEDULE_CRON`
  schedule — e.g. after lowering `RAW_RETENTION_INTERVAL` to test the
  pipeline against real data): `docker compose run --rm archive-job python
  export_parquet.py --once`. See `docs/archive-and-retention.md` for the
  full tier/retention breakdown.
- `make smoke-test` (`bin/smoke-test.sh`) builds and runs the real compose
  services end to end against a disposable broker — the check to reach for
  after touching a Dockerfile, `docker-compose.yml`, or secrets/env wiring,
  as distinct from `make test-integration`'s in-process/testcontainers
  coverage of decode/write correctness. `docs/regions.md` and
  `docs/gateway-agent.md` cover region configuration and gateway-agent
  deployment in operator-facing depth beyond this file's own summaries
  above.
- **This repo has no GitHub remote configured yet** (`git remote -v` is
  empty) — `.github/workflows/*.yml` and `renovate.json` are unverified
  against real GitHub Actions runs; only their YAML/JSON validity and the
  local pieces they invoke (`make lint`/`make test`/`make test-integration`/
  `make smoke-test`/`make proto-gen`/`make update-golden-metrics`, the
  metric-name-stability test itself) are confirmed. Once a remote exists,
  still needed before any of this is live: push the repo, add a
  `RENOVATE_TOKEN` secret (a PAT with repo access, for `renovate.yml`'s
  self-hosted run), and — in GitHub repo settings, not a file in this tree —
  enable branch protection on `main` requiring `ci.yml`'s four jobs
  (`lint`, `test`, `test-integration`, `smoke-test`) and
  `proto-regen-check.yml` as required status checks, plus enable the native
  merge queue (so `platformAutomerge: true` in `renovate.json` has a queue
  to enqueue into).
