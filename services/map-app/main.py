"""Read-only FastAPI backend for the telemetry map/playback frontend
(services/map-app/static/). Queries via the `grafana_ro` role — the same
read-only access Grafana's own datasource already uses (db/init/50_roles.sh)
— and serves the static frontend from the same origin, so no CORS wiring is
needed. This service holds no write path and no decode logic of its own.
"""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from meshdb_common.connect import build_grafana_ro_dsn, connect_with_retry

STATIC_DIR = Path(__file__).parent / "static"

# Numeric fields that exist but are meaningless as a color gauge (monotonic
# counters etc.) — extend as new noisy fields turn up.
METRIC_DENYLIST = {
    "device_metrics.uptime_seconds",
}

RAW_RESOLUTION_MAX_SECONDS = 48 * 3600
HOURLY_RESOLUTION_MAX_SECONDS = 14 * 24 * 3600


def choose_resolution(start: int, end: int) -> str:
    """Picks which of metric / metric_hourly / metric_daily a playback
    request should read from, based on the requested span — the same
    raw/hourly/daily tiering the Grafana dashboard's own panels lean on, so
    a wide scrub range doesn't pull hundreds of thousands of raw rows."""
    span = end - start
    if span <= RAW_RESOLUTION_MAX_SECONDS:
        return "raw"
    if span <= HOURLY_RESOLUTION_MAX_SECONDS:
        return "hourly"
    return "daily"


def rows_to_playback_payload(
    region: str,
    metric: str,
    resolution: str,
    start: int,
    end: int,
    position_rows: list[tuple],
    value_rows: list[tuple],
) -> dict:
    """Shapes raw SQL rows (node_id, t, ...) into the per-node
    {positions, values} wire format — kept separate from the query itself
    so it's testable with hand-built rows, no DB involved."""
    nodes: dict[int, dict[str, list]] = {}

    def node(node_id: int) -> dict[str, list]:
        return nodes.setdefault(node_id, {"positions": [], "values": []})

    for node_id, t, lat, lon, alt in position_rows:
        node(node_id)["positions"].append([t, lat, lon, alt])
    for node_id, t, value in value_rows:
        node(node_id)["values"].append([t, value])

    return {
        "region": region,
        "metric": metric,
        "resolution": resolution,
        "range": {"start": start, "end": end},
        "nodes": {str(node_id): data for node_id, data in nodes.items()},
    }


def create_app(*, conn: psycopg.Connection | None = None) -> FastAPI:
    """Factory rather than a bare module-level app so tests can inject an
    already-open (fake or real) connection instead of `lifespan` opening a
    real one — the container entrypoint below still gets a plain `app`."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.conn = conn if conn is not None else connect_with_retry(build_grafana_ro_dsn())
        app.state.owns_conn = conn is None
        app.state.lock = threading.Lock()
        try:
            yield
        finally:
            if app.state.owns_conn:
                app.state.conn.close()

    app = FastAPI(title="meshtastic-db map-app", lifespan=lifespan)

    def query(sql: str, params: tuple = ()) -> list[tuple]:
        # One shared connection serving concurrent read requests — a
        # synchronous psycopg connection isn't safe for concurrent cursor
        # use even when every query here is read-only (mirrors ingest-api's
        # own _write_lock around its one shared connection).
        with app.state.lock, app.state.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    @app.get("/api/regions")
    def regions() -> dict:
        rows = query(
            """
            SELECT n.region,
                   count(*) AS total,
                   count(*) FILTER (WHERE p.node_id IS NOT NULL) AS with_position
            FROM node_identity n
            LEFT JOIN LATERAL (
                SELECT node_id FROM node_position_history h WHERE h.node_id = n.node_id LIMIT 1
            ) p ON true
            WHERE n.region IS NOT NULL
            GROUP BY n.region
            ORDER BY n.region
            """
        )
        return {
            "regions": [
                {"region": region, "node_count": total, "with_position_count": with_position}
                for region, total, with_position in rows
            ]
        }

    @app.get("/api/nodes")
    def nodes(region: str) -> dict:
        rows = query(
            """
            SELECT n.node_id, n.long_name, n.short_name, n.hw_model, n.last_heard,
                   p.latitude, p.longitude, p.altitude, p.time
            FROM node_identity n
            LEFT JOIN LATERAL (
                SELECT latitude, longitude, altitude, time
                FROM node_position_history h
                WHERE h.node_id = n.node_id
                ORDER BY h.time DESC
                LIMIT 1
            ) p ON true
            WHERE n.region = %s
            ORDER BY n.node_id
            """,
            (region,),
        )
        result = []
        for node_id, long_name, short_name, hw_model, last_heard, lat, lon, alt, pos_time in rows:
            position = None
            if lat is not None:
                position = {"lat": lat, "lon": lon, "alt": alt, "time": int(pos_time.timestamp())}
            result.append(
                {
                    "node_id": node_id,
                    "long_name": long_name,
                    "short_name": short_name,
                    "hw_model": hw_model,
                    "last_heard": int(last_heard.timestamp()) if last_heard else None,
                    "position": position,
                }
            )
        return {"nodes": result}

    @app.get("/api/metrics")
    def metrics(region: str) -> dict:
        rows = query(
            """
            SELECT DISTINCT metric_name FROM metric_daily
            WHERE region = %s AND value_type = 'numeric'
            ORDER BY metric_name
            """,
            (region,),
        )
        names = [name for (name,) in rows if name not in METRIC_DENYLIST]
        if not names:
            # metric_daily/metric_hourly start WITH NO DATA and only
            # populate on their own refresh schedule — fall back to a
            # bounded raw scan so a fresh deployment isn't stuck empty.
            rows = query(
                """
                SELECT DISTINCT metric_name FROM metric
                WHERE region = %s AND value_type = 'numeric' AND time > now() - interval '7 days'
                ORDER BY metric_name
                """,
                (region,),
            )
            names = [name for (name,) in rows if name not in METRIC_DENYLIST]
        return {"metrics": names}

    @app.get("/api/playback")
    def playback(region: str, metric: str, start: int, end: int) -> dict:
        if end <= start:
            raise HTTPException(status_code=400, detail="end must be after start")

        resolution = choose_resolution(start, end)

        position_rows = query(
            """
            SELECT node_id, extract(epoch FROM time)::bigint AS t, latitude, longitude, altitude
            FROM node_position_history
            WHERE region = %s AND time BETWEEN to_timestamp(%s) AND to_timestamp(%s)
            ORDER BY node_id, time
            """,
            (region, start, end),
        )

        if resolution == "raw":
            value_rows = query(
                """
                SELECT node_id, extract(epoch FROM time)::bigint AS t, value_numeric
                FROM metric
                WHERE region = %s AND metric_name = %s AND value_type = 'numeric'
                  AND time BETWEEN to_timestamp(%s) AND to_timestamp(%s)
                ORDER BY node_id, time
                """,
                (region, metric, start, end),
            )
        elif resolution == "hourly":
            value_rows = query(
                """
                SELECT node_id, extract(epoch FROM hour)::bigint AS t, last_value_numeric
                FROM metric_hourly
                WHERE region = %s AND metric_name = %s AND last_value_numeric IS NOT NULL
                  AND hour BETWEEN to_timestamp(%s) AND to_timestamp(%s)
                ORDER BY node_id, hour
                """,
                (region, metric, start, end),
            )
        else:
            value_rows = query(
                """
                SELECT node_id, extract(epoch FROM day)::bigint AS t, last_value_numeric
                FROM metric_daily
                WHERE region = %s AND metric_name = %s AND last_value_numeric IS NOT NULL
                  AND day BETWEEN to_timestamp(%s) AND to_timestamp(%s)
                ORDER BY node_id, day
                """,
                (region, metric, start, end),
            )

        return rows_to_playback_payload(region, metric, resolution, start, end, position_rows, value_rows)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    # Registered last — Starlette matches routes in registration order, so
    # this catch-all mount must come after every /api/*//healthz route above.
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

    return app


app = create_app()
