"""Database writes with an in-memory buffer so a DB restart loses no readings."""
from __future__ import annotations

import json
import logging
from collections import deque

import asyncpg

from .parse import COUNTER_FIELDS, Reading

log = logging.getLogger(__name__)

COLS = Reading.columns()

class Store:
    def __init__(self, dsn: str, buffer_max: int = 6 * 3600):
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None
        self.buffer: deque[Reading] = deque(maxlen=buffer_max)
        self.copy_cols = COLS

    async def connect(self) -> None:
        if self.pool is None:
            self.pool = await asyncpg.create_pool(self.dsn, min_size=0, max_size=2, command_timeout=10)

    async def load_epoch_state(self) -> tuple[int, dict]:
        await self.connect()
        async with self.pool.acquire() as con:
            epoch = await con.fetchval("SELECT coalesce(max(epoch), 1) FROM meter_epoch")
            row = await con.fetchrow(
                f"SELECT {', '.join(COUNTER_FIELDS)} FROM reading_raw WHERE epoch = $1 ORDER BY ts DESC LIMIT 1",
                epoch)
        last = {k: v for k, v in dict(row or {}).items() if v is not None}
        return epoch, last

    def add(self, r: Reading) -> None:
        if len(self.buffer) == self.buffer.maxlen:
            log.warning("buffer full, dropping oldest reading")
        self.buffer.append(r)

    async def flush(self) -> int:
        """Write all buffered readings. Returns rows written; keeps them on error."""
        if not self.buffer:
            return 0
        batch = list(self.buffer)
        try:
            await self.connect()
            async with self.pool.acquire() as con:
                async with con.transaction():
                    # Staging table + ON CONFLICT so a retried flush never duplicates.
                    await con.execute(
                        "CREATE TEMP TABLE IF NOT EXISTS _stage (LIKE reading_raw) ON COMMIT DELETE ROWS")
                    await con.copy_records_to_table("_stage", records=[r.row() for r in batch],
                                                    columns=self.copy_cols)
                    await con.execute(
                        "INSERT INTO reading_raw SELECT * FROM _stage ON CONFLICT (ts) DO NOTHING")
        except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError) as e:
            log.warning("flush failed (%d buffered): %s", len(self.buffer), e)
            await self._reset_pool()
            return 0
        for _ in batch:
            self.buffer.popleft()
        return len(batch)

    async def event(self, kind: str, message: str, severity: str = "info", data: dict | None = None) -> None:
        try:
            await self.connect()
            await self.pool.execute(
                "INSERT INTO event (kind, severity, message, data) VALUES ($1, $2, $3, $4::jsonb)",
                kind, severity, message, json.dumps(data or {}, default=str))
        except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError) as e:
            log.warning("could not write event %s: %s", kind, e)

    async def new_epoch(self, epoch: int, reason: str) -> None:
        try:
            await self.connect()
            await self.pool.execute(
                "INSERT INTO meter_epoch (epoch, reason) VALUES ($1, $2) ON CONFLICT DO NOTHING", epoch, reason)
        except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError) as e:
            log.warning("could not write epoch: %s", e)

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
        except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError) as e:
            log.debug("heartbeat failed: %s", e)

    async def _reset_pool(self) -> None:
        if self.pool is not None:
            try:
                self.pool.terminate()
            finally:
                self.pool = None
