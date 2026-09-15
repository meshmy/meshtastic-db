"""Thin FastAPI front door for remote gateway-agent instances that can't
reach Postgres directly (§1/§5 of the design): `POST /v1/ingest` takes a
batch of already-decoded envelopes (JSON, meshdb_common.serialization's
wire format) and calls the same write_envelopes() every other ingestion
source uses — this service holds no decode logic of its own.

Region opt-in is enforced here too, as defense in depth against a
misconfigured gateway-agent tagging the wrong region (the same pattern
mqtt-ingest/tcp-poller already apply to their own transports).
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import asynccontextmanager

import psycopg
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from meshdb_common.config import RegionsConfig, load_regions_config, resolve_secret
from meshdb_common.connect import build_ingest_dsn, connect_with_retry
from meshdb_common.db import write_envelopes
from meshdb_common.regions import is_region_allowed
from meshdb_common.serialization import envelope_from_dict

logger = logging.getLogger("ingest-api")

_bearer = HTTPBearer(auto_error=False)
_write_lock = threading.Lock()


def _check_token(credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:
    expected = resolve_secret("INGEST_API_TOKEN")
    if not expected or credentials is None or credentials.credentials != expected:
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


def create_app(*, conn: psycopg.Connection | None = None, cfg: RegionsConfig | None = None) -> FastAPI:
    """Factory rather than a bare module-level app so tests can inject an
    already-open test connection/config instead of `lifespan` opening a
    real one — the container entrypoint below still gets a plain `app`."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.conn = conn if conn is not None else connect_with_retry(build_ingest_dsn())
        app.state.cfg = cfg if cfg is not None else load_regions_config(os.environ.get("REGIONS_CONFIG_PATH", "/config/regions.yaml"))
        app.state.owns_conn = conn is None
        try:
            yield
        finally:
            if app.state.owns_conn:
                app.state.conn.close()

    app = FastAPI(title="meshtastic-db ingest-api", lifespan=lifespan)

    @app.post("/v1/ingest")
    def ingest(body: list[dict], _: None = Depends(_check_token)) -> dict:
        try:
            envelopes = [envelope_from_dict(item) for item in body]
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"malformed envelope: {exc}") from exc

        allowed = []
        for env in envelopes:
            if is_region_allowed(env.region, app.state.cfg):
                allowed.append(env)
            else:
                logger.warning("dropping envelope for disallowed region %r (node %s)", env.region, env.node_id)

        if allowed:
            with _write_lock:
                write_envelopes(app.state.conn, allowed)
        logger.info("wrote %d/%d envelope(s) via ingest-api", len(allowed), len(envelopes))
        return {"received": len(envelopes), "written": len(allowed)}

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
