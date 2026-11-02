"""Database writes with an in-memory buffer so a DB restart loses no readings.

Nothing here is awaited by the meter polling loop: readings, events and epoch
records are queued in memory and written by the collector's writer task.
Queued events and epochs are retried together with the readings, in the same
transaction, so an outage cannot persist readings while losing their epoch.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from datetime import datetime, timezone

import asyncpg

from .parse import COUNTER_FIELDS, Reading

log = logging.getLogger(__name__)

COLS = Reading.columns()
DB_ERRORS = (OSError, asyncio.TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError)

class Store:
    def __init__(self, dsn: str, buffer_max: int = 6 * 3600):
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None
        self.buffer: deque[Reading] = deque(maxlen=buffer_max)
        self.copy_cols = COLS
        self.pending: deque[tuple[str, tuple]] = deque(maxlen=10_000)  # events, epochs

    async def connect(self) -> None:
        if self.pool is None:
            self.pool = await asyncpg.create_pool(self.dsn, min_size=0, max_size=2,
                                                  command_timeout=10, timeout=5)

    async def load_epoch_state(self) -> tuple[int, dict]:
        await self.connect()
        async with self.pool.acquire() as con:
            # Readings are the source of truth; meter_epoch is descriptive.
            epoch = await con.fetchval(
                "SELECT greatest((SELECT max(epoch) FROM meter_epoch), (SELECT max(epoch) FROM reading_raw), 1)")
            row = await con.fetchrow(
                f"SELECT {', '.join(COUNTER_FIELDS)} FROM reading_raw WHERE epoch = $1 ORDER BY ts DESC LIMIT 1",
                epoch)
        last = {k: v for k, v in dict(row or {}).items() if v is not None}
        return epoch, last

    async def get_setting(self, key: str):
        await self.connect()
        v = await self.pool.fetchval("SELECT value FROM setting WHERE key = $1", key)
        return json.loads(v) if isinstance(v, str) else v

    def add(self, r: Reading) -> None:
        if len(self.buffer) == self.buffer.maxlen:
            log.warning("buffer full, dropping oldest reading")
        self.buffer.append(r)

    async def flush(self) -> int:
        """Write queued events/epochs and buffered readings in one transaction.

        Returns readings written; on error everything stays queued for the next try.
        """
        if not self.buffer and not self.pending:
            return 0
        batch = list(self.buffer)
        ops = list(self.pending)
        try:
            await self.connect()
            async with self.pool.acquire() as con:
                async with con.transaction():
                    for sql, args in ops:
                        await con.execute(sql, *args)
                    if batch:
                        # Staging table + ON CONFLICT so a retried flush never duplicates.
                        await con.execute(
                            "CREATE TEMP TABLE IF NOT EXISTS _stage (LIKE reading_raw) ON COMMIT DELETE ROWS")
                        await con.copy_records_to_table("_stage", records=[r.row() for r in batch],
                                                        columns=self.copy_cols)
                        await con.execute(
                            "INSERT INTO reading_raw SELECT * FROM _stage ON CONFLICT (ts) DO NOTHING")
        except DB_ERRORS as e:
            log.warning("flush failed (%d readings, %d events queued): %s", len(self.buffer), len(self.pending), e)
            await self._reset_pool()
            return 0
        for _ in batch:
            self.buffer.popleft()
        for _ in ops:
            self.pending.popleft()
        return len(batch)

    def event(self, kind: str, message: str, severity: str = "info", data: dict | None = None) -> None:
        self.pending.append(("INSERT INTO event (ts, kind, severity, message, data) VALUES ($1, $2, $3, $4, $5::jsonb)",
                             (datetime.now(timezone.utc), kind, severity, message,
                              json.dumps(data or {}, default=str))))

    def new_epoch(self, epoch: int, reason: str) -> None:
        self.pending.append(("INSERT INTO meter_epoch (epoch, started_at, reason) VALUES ($1, $2, $3) "
                             "ON CONFLICT DO NOTHING", (epoch, datetime.now(timezone.utc), reason)))

    async def heartbeat(self, **fields) -> None:
        fields["buffered_readings"] = len(self.buffer)
        if "meter_info" in fields:
            fields["meter_info"] = json.dumps(fields["meter_info"])
        sets = ", ".join(f"{k} = ${i + 1}" + ("::jsonb" if k == "meter_info" else "")
                         for i, k in enumerate(fields))
        try:
            await self.connect()
            await self.pool.execute(f"UPDATE collector_status SET updated_at = now(), {sets} WHERE id = 1",
                                    *fields.values())
        except DB_ERRORS as e:
            log.debug("heartbeat failed: %s", e)

    async def _reset_pool(self) -> None:
        if self.pool is not None:
            try:
                self.pool.terminate()
            finally:
                self.pool = None
