"""Buffers decoded envelopes in memory and flushes them to Postgres via
`db.write_envelopes` on a size/time threshold — shared by every ingestion
service that streams envelopes one at a time off a live connection (MQTT,
TCP) rather than receiving them already batched."""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter

import psycopg

from .db import write_envelopes
from .envelope import DecodedPacketEnvelope

logger = logging.getLogger(__name__)


class EnvelopeBatcher:
    """Buffers decoded envelopes and flushes them to Postgres via
    write_envelopes() once `batch_size` is reached or `batch_interval`
    seconds have passed since the oldest buffered envelope — bounds both
    memory and worst-case write latency. Thread-safe: `add()` is expected to
    run on a transport's own network/reader thread, `flush_if_due()` polled
    from the main thread."""

    def __init__(self, conn: psycopg.Connection, *, batch_size: int, batch_interval: float) -> None:
        self._conn = conn
        self._batch_size = batch_size
        self._batch_interval = batch_interval
        self._lock = threading.Lock()
        self._buffer: list[DecodedPacketEnvelope] = []
        self._oldest_at: float | None = None

    def add(self, envelope: DecodedPacketEnvelope) -> None:
        with self._lock:
            self._buffer.append(envelope)
            if self._oldest_at is None:
                self._oldest_at = time.monotonic()
            should_flush = len(self._buffer) >= self._batch_size
        if should_flush:
            self.flush()

    def flush_if_due(self) -> None:
        with self._lock:
            due = self._oldest_at is not None and (time.monotonic() - self._oldest_at) >= self._batch_interval
        if due:
            self.flush()

    def flush(self) -> None:
        with self._lock:
            batch, self._buffer = self._buffer, []
            self._oldest_at = None
        if batch:
            write_envelopes(self._conn, batch)
            by_packet_type = dict(Counter(env.packet_type for env in batch))
            logger.info("wrote batch of %d envelope(s): %s", len(batch), by_packet_type)
