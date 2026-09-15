SELECT create_hypertable('metric', 'time', chunk_time_interval => INTERVAL '1 day');

CREATE INDEX idx_metric_node_metric_time ON metric (node_id, metric_name, time DESC);
CREATE INDEX idx_metric_region_time      ON metric (region, time DESC);
CREATE INDEX idx_metric_packet_type_time ON metric (packet_type, time DESC);

-- GIS interoperability: a generated column derived from the existing
-- lat/long floats, zero ingestion-code involvement.
ALTER TABLE metric ADD COLUMN geom GEOGRAPHY(Point, 4326)
  GENERATED ALWAYS AS (
    CASE WHEN latitude IS NOT NULL AND longitude IS NOT NULL
      THEN ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography
    END
  ) STORED;
CREATE INDEX idx_metric_geom ON metric USING GIST (geom);

SELECT create_hypertable('node_position_history', 'time', chunk_time_interval => INTERVAL '7 days');
CREATE INDEX idx_position_node_time ON node_position_history (node_id, time DESC);

ALTER TABLE node_position_history ADD COLUMN geom GEOGRAPHY(Point, 4326)
  GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography) STORED;
CREATE INDEX idx_position_geom ON node_position_history USING GIST (geom);
