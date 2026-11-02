"""Backend API: auth, live/history data, status, and the web app (SPA)."""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse

from . import auth, data, insights

WEB_DIR = Path(os.environ.get("WEB_DIR", Path(__file__).parent / "web"))
COLLECTOR_STALE_S = 120  # F-ALERT-04: no data for > 2 min


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=5)
    yield
    await app.state.pool.close()


app = FastAPI(title="P1 Energy Monitor", version="0.3.0", lifespan=lifespan,
              docs_url="/api/docs", openapi_url="/api/openapi.json")
app.include_router(auth.router)
app.include_router(data.router)
app.include_router(insights.router)


def _iso(v):
    return v.isoformat() if isinstance(v, datetime) else v


@app.get("/healthz", include_in_schema=False)
async def healthz():
    async with app.state.pool.acquire() as con:
        await con.fetchval("SELECT 1")
    return {"ok": True}


@app.get("/api/status", summary="System status (F-SET-03)", tags=["status"],
         dependencies=[Depends(auth.current_user)])
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


@app.get("/api/{path:path}", include_in_schema=False)
async def api_404(path: str):
    raise HTTPException(404, "not found")


@app.get("/{path:path}", include_in_schema=False)
async def spa(path: str):
    """Serve the built web app; unknown paths fall back to index.html."""
    f = (WEB_DIR / path).resolve()
    if path and f.is_file() and WEB_DIR.resolve() in f.parents:
        headers = {"Cache-Control": "public, max-age=31536000, immutable"} if path.startswith("assets/") else {}
        return FileResponse(f, headers=headers)
    index = WEB_DIR / "index.html"
    if not index.is_file():
        raise HTTPException(503, "web app not built (run npm run build in frontend/)")
    return FileResponse(index, headers={"Cache-Control": "no-cache"})
