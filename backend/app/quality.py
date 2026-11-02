"""Power quality (F-QUAL): voltage per phase over time, daily min/max,
sags/swells and power failures."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request

from .auth import current_user
from .data import TZ, _range, _row

router = APIRouter(prefix="/api/quality", tags=["quality"], dependencies=[Depends(current_user)])

PHASES = (1, 2, 3)
QUALITY_EVENTS = ("power_failure", "voltage_sag", "voltage_swell", "meter_offline", "meter_online")


def _phase_cols(prefix: str = "") -> str:
    return ", ".join(f"min({prefix}voltage_l{i}_v) AS l{i}_min_v, avg({prefix}voltage_l{i}_v) AS l{i}_avg_v, "
                     f"max({prefix}voltage_l{i}_v) AS l{i}_max_v" for i in PHASES)


@router.get("/voltage", summary="Voltage per phase with min/max band (5 min, 1 h or 1 day)")
async def voltage(request: Request, start: datetime, end: datetime):
    start, end = _range(start, end)
    pool = request.app.state.pool
    span = end - start
    if span <= timedelta(days=3) and start > datetime.now(timezone.utc) - timedelta(days=30):
        res = "5m"
        sql = (f"SELECT time_bucket(interval '5 minutes', ts) AS t, {_phase_cols()} FROM reading_raw "
               "WHERE ts >= $1 AND ts < $2 GROUP BY 1 ORDER BY 1")
    elif span <= timedelta(days=93):
        res = "1h"
        sql = ("SELECT bucket AS t, " + ", ".join(f"l{i}_min_v, l{i}_avg_v, l{i}_max_v" for i in PHASES) +
               " FROM quality_1h WHERE bucket >= $1 AND bucket < $2 ORDER BY 1")
    else:
        res = "1d"
        sql = (f"SELECT time_bucket(interval '1 day', bucket, '{TZ}') AS t, " +
               ", ".join(f"min(l{i}_min_v) AS l{i}_min_v, sum(l{i}_avg_v * samples) / nullif(sum(samples), 0) "
                         f"AS l{i}_avg_v, max(l{i}_max_v) AS l{i}_max_v" for i in PHASES) +
               " FROM quality_1h WHERE bucket >= $1 AND bucket < $2 GROUP BY 1 ORDER BY 1")
    rows = await pool.fetch(sql, start, end)
    phases = [i for i in PHASES if any(r[f"l{i}_avg_v"] is not None for r in rows)]
    return {"resolution": res, "phases": phases, "rows": [_row(r) for r in rows]}


@router.get("/daily", summary="Per local day: voltage min/max per phase, sags, swells, power failures")
async def daily(request: Request, start: datetime, end: datetime):
    start, end = _range(start, end)
    # Counter deltas per day; the previous day's last value is the baseline so
    # events right after midnight are not lost (the first day with data uses its
    # own first hour). Negative deltas (meter swap) count as 0.
    counters = ["sag_l1_count", "sag_l2_count", "sag_l3_count", "swell_l1_count", "swell_l2_count",
                "swell_l3_count", "power_fail_count", "long_power_fail_count"]
    sql = f"""
        WITH d AS (
            SELECT time_bucket(interval '1 day', bucket, '{TZ}') AS day,
                   {", ".join(f"min(l{i}_min_v) AS l{i}_min_v, max(l{i}_max_v) AS l{i}_max_v" for i in PHASES)},
                   {", ".join(f"last({c}, bucket) AS {c}, first({c}, bucket) AS {c}_first" for c in counters)}
            FROM quality_1h
            WHERE bucket >= $1::timestamptz - interval '1 day' AND bucket < $2
            GROUP BY 1
        )
        SELECT day AS t, {", ".join(f"l{i}_min_v, l{i}_max_v" for i in PHASES)},
               {", ".join(f"greatest(0, {c} - coalesce(lag({c}) OVER w, {c}_first)) AS {c.replace('_count', '')}"
                         for c in counters)}
        FROM d WINDOW w AS (ORDER BY day)
        ORDER BY day"""
    rows = [r for r in await request.app.state.pool.fetch(sql, start, end) if r["t"] >= start]
    out = []
    for r in rows:
        x = _row(r)
        x["sags"] = sum(x.pop(f"sag_l{i}") or 0 for i in PHASES)
        x["swells"] = sum(x.pop(f"swell_l{i}") or 0 for i in PHASES)
        out.append(x)
    return {"rows": out}


@router.get("/events", summary="Power failures, voltage sags/swells and meter outages")
async def events(request: Request, limit: int = Query(100, ge=1, le=1000)):
    rows = await request.app.state.pool.fetch(
        "SELECT id, ts, kind, severity, message, data FROM event WHERE kind = ANY($1::text[]) "
        "ORDER BY ts DESC LIMIT $2", list(QUALITY_EVENTS), limit)
    return [_row(r) for r in rows]
