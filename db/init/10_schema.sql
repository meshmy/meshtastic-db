-- Bare table structure only. Hypertable conversion, indexes, and the
-- generated `geom` columns live in 20_hypertable.sql (they depend on the
-- tables created here and, for `geom`, on the postgis extension from
-- 00_extensions.sql).

CREATE TABLE metric (
    time            TIMESTAMPTZ      NOT NULL,   -- packet's own rx_time
    node_id         BIGINT           NOT NULL,   -- node num is uint32; BIGINT avoids overflow
    region          TEXT             NOT NULL,
    gateway_node_id BIGINT,                      -- MQTT uplink node, if known
    packet_type     TEXT             NOT NULL,   -- 'Telemetry','Position','Routing','NeighborInfo',...
    portnum         SMALLINT         NOT NULL,
    metric_name     TEXT             NOT NULL,   -- dotted field path, e.g. 'telemetry.environment_metrics.temperature'
    value_type      TEXT             NOT NULL CHECK (value_type IN ('numeric','bool','text','enum')),
    value_numeric   DOUBLE PRECISION,
    value_text      TEXT,
    value_bool      BOOLEAN,
    snr             REAL,
    rssi            SMALLINT,
    hop_limit       SMALLINT,
    hop_start       SMALLINT,
    channel         SMALLINT,
    source          TEXT             NOT NULL,   -- 'mqtt' | 'ble' | 'serial' | 'tcp'
    packet_id       BIGINT,
    -- resolved at write time, as-of the packet's own timestamp:
    latitude        DOUBLE PRECISION,
    longitude       DOUBLE PRECISION,
    altitude        REAL,
    position_time   TIMESTAMPTZ,                 -- timestamp of the position fix actually used; NULL if never seen
    PRIMARY KEY (time, node_id, metric_name, packet_id)
);

CREATE TABLE node_position_history (
    time            TIMESTAMPTZ NOT NULL,
    node_id         BIGINT NOT NULL,
    region          TEXT NOT NULL,
    latitude        DOUBLE PRECISION NOT NULL,
    longitude       DOUBLE PRECISION NOT NULL,
    altitude        REAL,
    location_source TEXT,
    ground_speed    REAL,
    ground_track    REAL,
    PRIMARY KEY (time, node_id)
);

-- Plain table, not a hypertable: latest-wins node identity, upserted from
-- any source that decodes a User/NodeInfo packet.
CREATE TABLE node_identity (
    node_id       BIGINT PRIMARY KEY,
    long_name     TEXT,
    short_name    TEXT,
    hw_model      TEXT,
    role          TEXT,
    is_licensed   BOOLEAN,
    region        TEXT,
    last_heard    TIMESTAMPTZ,
    first_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_node_identity_last_heard ON node_identity (last_heard DESC);

-- Plain table: bookkeeping for the archive job.
CREATE TABLE archive_manifest (
    chunk_name    TEXT PRIMARY KEY,
    range_start   TIMESTAMPTZ NOT NULL,
    range_end     TIMESTAMPTZ NOT NULL,
    parquet_path  TEXT NOT NULL,
    row_count     BIGINT NOT NULL,
    sha256        TEXT NOT NULL,
    exported_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    dropped_at    TIMESTAMPTZ
);
