"""Live, history, power and CSV export endpoints (F-LIVE, F-HIST, F-EXP-01)."""
from __future__ import annotations

import asyncio
import csv
import io
import json
from datetime import datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from .auth import current_user

router = APIRouter(prefix="/api", tags=["data"], dependencies=[Depends(current_user)])
TZ = "Europe/Amsterdam"
ZONE = ZoneInfo(TZ)

LIVE_COLS = ("ts, power_w, power_l1_w, power_l2_w, power_l3_w, voltage_l1_v, voltage_l2_v, voltage_l3_v, "
             "current_l1_a, current_l2_a, current_l3_a, active_tariff, import_t1_kwh, import_t2_kwh, "
             "export_t1_kwh, export_t2_kwh, gas_m3, gas_ts")

Resolution = Literal["15m", "1h", "1d", "1mo", "1y"]

# Energy rows per resolution. 1mo/1y are built from local calendar days.
HISTORY_SQL = {
    "15m": "SELECT bucket AS t, import_t1_kwh, import_t2_kwh, export_t1_kwh, export_t2_kwh, gas_m3, power_avg_w "
           "FROM energy_15m WHERE bucket >= $1 AND bucket < $2 ORDER BY 1",
    "1h": "SELECT bucket AS t, import_t1_kwh, import_t2_kwh, export_t1_kwh, export_t2_kwh, gas_m3, power_avg_w "
          "FROM energy_1h WHERE bucket >= $1 AND bucket < $2 ORDER BY 1",
    "1d": "SELECT bucket AS t, import_t1_kwh, import_t2_kwh, export_t1_kwh, export_t2_kwh, gas_m3, power_avg_w "
          "FROM energy_1d WHERE bucket >= $1 AND bucket < $2 ORDER BY 1",
}
for _res, _unit in (("1mo", "month"), ("1y", "year")):
    HISTORY_SQL[_res] = (
        f"SELECT date_trunc('{_unit}', bucket AT TIME ZONE '{TZ}') AT TIME ZONE '{TZ}' AS t, "
        "sum(import_t1_kwh) AS import_t1_kwh, sum(import_t2_kwh) AS import_t2_kwh, "
        "sum(export_t1_kwh) AS export_t1_kwh, sum(export_t2_kwh) AS export_t2_kwh, "
        "sum(gas_m3) AS gas_m3, avg(power_avg_w) AS power_avg_w "
        "FROM energy_1d WHERE bucket >= $1 AND bucket < $2 GROUP BY 1 ORDER BY 1")

ENERGY_KEYS = ("import_t1_kwh", "import_t2_kwh", "export_t1_kwh", "export_t2_kwh", "gas_m3")


def _row(r) -> dict:
    out = {}
    for k, v in dict(r).items():
        out[k] = v.isoformat() if isinstance(v, datetime) else (round(v, 4) if isinstance(v, float) else v)
    return out


def _range(start: datetime, end: datetime, max_days: int | None = None) -> tuple[datetime, datetime]:
    if start.tzinfo is None or end.tzinfo is None:
        raise HTTPException(422, "start and end need a timezone offset")
    if end <= start:
        raise HTTPException(422, "end must be after start")
    if max_days and end - start > timedelta(days=max_days):
        raise HTTPException(422, f"range too long for this resolution (max {max_days} days)")
    return start, end


def _totals(rows: list[dict]) -> dict:
    t = {k: round(sum(r.get(k) or 0 for r in rows), 4) for k in ENERGY_KEYS}
    t["import_kwh"] = round(t["import_t1_kwh"] + t["import_t2_kwh"], 4)
    t["export_kwh"] = round(t["export_t1_kwh"] + t["export_t2_kwh"], 4)
    t["net_kwh"] = round(t["import_kwh"] - t["export_kwh"], 4)
    return t


def _floor_day(ts: datetime) -> datetime:
    """Local (Amsterdam) midnight at or before ts, as UTC."""
    local = ts.astimezone(ZONE)
    return datetime(local.year, local.month, local.day, tzinfo=ZONE).astimezone(timezone.utc)


def _ceil_day(ts: datetime) -> datetime:
    """Local (Amsterdam) midnight at or after ts, as UTC."""
    day = _floor_day(ts)
    if day == ts:
        return day
    nxt = (day.astimezone(ZONE) + timedelta(days=1, hours=12)).date()
    return datetime(nxt.year, nxt.month, nxt.day, tzinfo=ZONE).astimezone(timezone.utc)


async def history_rows(pool, resolution: str, start: datetime, end: datetime) -> list[dict]:
    rows = await pool.fetch(HISTORY_SQL[resolution], start, end)
    return [_row(r) for r in rows]


# --- live ------------------------------------------------------------------

@router.get("/live", summary="Latest raw reading")
async def live(request: Request):
    row = await request.app.state.pool.fetchrow(f"SELECT {LIVE_COLS} FROM reading_raw ORDER BY ts DESC LIMIT 1")
    if row is None:
        raise HTTPException(404, "no readings yet")
    return _row(row)


@router.get("/live/stream", summary="Server-sent events with each new reading (1 s)")
async def live_stream(request: Request):
    pool = request.app.state.pool

    async def gen():
        last = None
        while not await request.is_disconnected():
            row = await pool.fetchrow(f"SELECT {LIVE_COLS} FROM reading_raw ORDER BY ts DESC LIMIT 1")
            if row and row["ts"] != last:
                last = row["ts"]
                yield f"data: {json.dumps(_row(row))}\n\n"
            else:
                yield ": keepalive\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/live/recent", summary="Recent 1 s power readings for the rolling graph")
async def live_recent(request: Request, seconds: int = Query(3600, ge=10, le=6 * 3600)):
    rows = await request.app.state.pool.fetch(
        "SELECT ts, power_w, power_l1_w, power_l2_w, power_l3_w FROM reading_raw "
        "WHERE ts > now() - make_interval(secs => $1) ORDER BY ts", seconds)
    return {"columns": ["ts", "power_w", "power_l1_w", "power_l2_w", "power_l3_w"],
            "rows": [[r["ts"].isoformat(), r["power_w"], r["power_l1_w"], r["power_l2_w"], r["power_l3_w"]]
                     for r in rows]}


@router.get("/today", summary="Energy totals for the current local day")
async def today(request: Request):
    pool = request.app.state.pool
    start = await pool.fetchval(f"SELECT time_bucket(interval '1 day', now(), '{TZ}')")
    rows = await history_rows(pool, "15m", start, start + timedelta(days=2))
    return {"start": start.isoformat(), **_totals(rows)}


# --- history ---------------------------------------------------------------

@router.get("/history", summary="Energy per bucket from counter deltas")
async def history(request: Request, resolution: Resolution, start: datetime, end: datetime):
    max_days = {"15m": 93, "1h": 400}.get(resolution)
    start, end = _range(start, end, max_days)
    rows = await history_rows(request.app.state.pool, resolution, start, end)
    return {"resolution": resolution, "start": start.isoformat(), "end": end.isoformat(),
            "rows": rows, "totals": _totals(rows)}


@router.get("/history/totals", summary="Energy totals for a period (for comparisons)")
async def history_totals(request: Request, start: datetime, end: datetime):
    start, end = _range(start, end)
    pool = request.app.state.pool
    first_day, last_day = _ceil_day(start), _floor_day(end)
    if first_day < last_day:
        # Whole local days from energy_1d, the partial edge days from energy_15m.
        rows = await history_rows(pool, "1d", first_day, last_day)
        if start < first_day:
            rows += await history_rows(pool, "15m", start, first_day)
        if last_day < end:
            rows += await history_rows(pool, "15m", last_day, end)
    else:
        rows = await history_rows(pool, "15m", start, end)
    return {"start": start.isoformat(), "end": end.isoformat(), **_totals(rows)}


@router.get("/history/range", summary="First and last available data")
async def history_range(request: Request):
    row = await request.app.state.pool.fetchrow("SELECT min(bucket) AS first, max(bucket) AS last FROM counter_15m")
    return _row(row)


@router.get("/power", summary="Power series; resolution picked from the range (1 s, 1 min or 15 min)")
async def power(request: Request, start: datetime, end: datetime):
    start, end = _range(start, end)
    pool = request.app.state.pool
    now = datetime.now(timezone.utc)
    span = end - start
    if span <= timedelta(hours=6) and start > now - timedelta(days=30):
        res, sql = "1s", ("SELECT ts AS t, power_w AS avg, power_w AS min, power_w AS max FROM reading_raw "
                          "WHERE ts >= $1 AND ts < $2 ORDER BY 1")
    elif span <= timedelta(days=14) and start > now - timedelta(days=730):
        res, sql = "1m", ("SELECT bucket AS t, power_avg_w AS avg, power_min_w AS min, power_max_w AS max "
                          "FROM reading_1m WHERE bucket >= $1 AND bucket < $2 ORDER BY 1")
    else:
        res, sql = "15m", ("SELECT bucket AS t, power_avg_w AS avg, NULL::float8 AS min, NULL::float8 AS max "
                           "FROM counter_15m WHERE bucket >= $1 AND bucket < $2 ORDER BY 1")
    rows = await pool.fetch(sql, start, end)
    return {"resolution": res, "rows": [[r["t"].isoformat(), r["avg"], r["min"], r["max"]] for r in rows]}


# --- export ----------------------------------------------------------------

ExportResolution = Literal["1s", "1m", "15m", "1h", "1d", "1mo", "1y"]


@router.get("/export.csv", summary="CSV export per period and resolution (F-EXP-01)", tags=["export"])
async def export_csv(request: Request, resolution: ExportResolution, start: datetime, end: datetime):
    max_days = {"1s": 7, "1m": 93}.get(resolution)
    start, end = _range(start, end, max_days)
    pool = request.app.state.pool
    if resolution == "1s":
        records = await pool.fetch(f"SELECT {LIVE_COLS} FROM reading_raw WHERE ts >= $1 AND ts < $2 ORDER BY ts",
                                   start, end)
    elif resolution == "1m":
        records = await pool.fetch(
            "SELECT bucket AS ts, power_avg_w, power_min_w, power_max_w, power_l1_avg_w, power_l2_avg_w, "
            "power_l3_avg_w, voltage_min_v, voltage_max_v, import_t1_kwh, import_t2_kwh, export_t1_kwh, "
            "export_t2_kwh, gas_m3 FROM reading_1m WHERE bucket >= $1 AND bucket < $2 ORDER BY 1", start, end)
    else:
        records = await pool.fetch(HISTORY_SQL[resolution], start, end)

    buf = io.StringIO()
    w = csv.writer(buf)
    if records:
        cols = list(records[0].keys())
        w.writerow(["time_local", *cols])
        for r in records:
            w.writerow([_local(r[cols[0]]), *[r[c].isoformat() if isinstance(r[c], datetime) else r[c] for c in cols]])
    else:
        w.writerow(["no data"])
    name = f"p1-{resolution}-{start.date()}-{end.date()}.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _local(t: datetime) -> str:
    return t.astimezone(ZONE).strftime("%Y-%m-%d %H:%M:%S")
