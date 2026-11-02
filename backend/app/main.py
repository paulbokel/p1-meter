"""Backend API. Phase 1: health, status and latest reading."""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

STATIC = Path(__file__).parent / "static"
COLLECTOR_STALE_S = 120  # F-ALERT-04: no data for > 2 min


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=5)
    yield
    await app.state.pool.close()


app = FastAPI(title="P1 Energy Monitor", version="0.1.0", lifespan=lifespan,
              docs_url="/api/docs", openapi_url="/api/openapi.json")


def _iso(v):
    return v.isoformat() if isinstance(v, datetime) else v


@app.get("/healthz", include_in_schema=False)
async def healthz():
    async with app.state.pool.acquire() as con:
        await con.fetchval("SELECT 1")
    return {"ok": True}


@app.get("/api/live", summary="Latest raw reading")
async def live():
    row = await app.state.pool.fetchrow("SELECT * FROM reading_raw ORDER BY ts DESC LIMIT 1")
    if row is None:
        raise HTTPException(404, "no readings yet")
    return {k: _iso(v) for k, v in dict(row).items()}


@app.get("/api/status", summary="System status (F-SET-03)")
async def status():
    pool = app.state.pool
    async with pool.acquire() as con:
        c = dict(await con.fetchrow("SELECT * FROM collector_status WHERE id = 1"))
        db_size = await con.fetchval("SELECT pg_database_size(current_database())")
        stats = await con.fetchrow("""
            SELECT
              (SELECT count(*) FROM reading_raw WHERE ts > now() - interval '1 hour')  AS readings_last_hour,
              (SELECT min(ts) FROM reading_raw)                                         AS oldest_raw,
              (SELECT min(bucket) FROM counter_15m)                                     AS history_since,
              (SELECT max(epoch) FROM meter_epoch)                                      AS epoch
        """)
        backup = await con.fetchrow("SELECT * FROM backup_run ORDER BY started_at DESC LIMIT 1")
        events = await con.fetch("SELECT ts, kind, severity, message FROM event ORDER BY ts DESC LIMIT 20")
        migrations = await con.fetch("SELECT version, applied_at FROM schema_migrations ORDER BY version")
        today = await con.fetchrow("""
            SELECT sum(import_t1_kwh + import_t2_kwh) AS import_kwh,
                   sum(export_t1_kwh + export_t2_kwh) AS export_kwh,
                   sum(gas_m3) AS gas_m3
            FROM energy_15m
            WHERE bucket >= time_bucket(interval '1 day', now(), 'Europe/Amsterdam')
        """)

    now = datetime.now(timezone.utc)
    last = c.get("last_reading_at")
    age = (now - last).total_seconds() if last else None
    heartbeat_age = (now - c["updated_at"]).total_seconds()
    healthy = bool(c["online"] and age is not None and age < COLLECTOR_STALE_S and heartbeat_age < 60)
    meter_info = c.pop("meter_info") or {}
    if isinstance(meter_info, str):
        meter_info = json.loads(meter_info)

    return {
        "now": now.isoformat(),
        "healthy": healthy,
        "collector": {**{k: _iso(v) for k, v in c.items()},
                      "last_reading_age_s": age, "heartbeat_age_s": heartbeat_age},
        "meter": meter_info,
        "database": {"size_bytes": db_size, **{k: _iso(v) for k, v in dict(stats).items()},
                     "migrations": [r["version"] for r in migrations]},
        "today": {k: (round(v, 3) if v is not None else None) for k, v in dict(today).items()},
        "backup": {k: _iso(v) for k, v in dict(backup).items()} if backup else None,
        "events": [{k: _iso(v) for k, v in dict(e).items()} for e in events],
    }


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC / "status.html")
