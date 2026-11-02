"""Phase 3 API: contracts & costs, prices, base load, alert rules, channels,
web push and events."""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from statistics import median
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from . import alerts, baseload, costs, notify
from .auth import User, current_user, require_admin
from .costs import ZONE

router = APIRouter(prefix="/api", dependencies=[Depends(current_user)])


def _j(v):
    if isinstance(v, (datetime, date, time)):
        return v.isoformat()
    if hasattr(v, "as_tuple"):  # Decimal
        return float(v)
    if isinstance(v, float):
        return round(v, 4)
    return v


def _row(r) -> dict:
    d = dict(r)
    for k in ("params", "state", "config", "data", "keys"):
        if isinstance(d.get(k), str):
            d[k] = json.loads(d[k])
    return {k: _j(v) for k, v in d.items()}


def _range(start: datetime, end: datetime):
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise HTTPException(422, "start/end need a timezone offset and end > start")
    return start, end


# --- contracts ---------------------------------------------------------------

class ContractIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    supplier: str | None = Field(default=None, max_length=100)
    kind: Literal["fixed", "daynight", "dynamic"]
    valid_from: date
    valid_to: date | None = None
    elec_import_t1: float = Field(ge=-5, le=5)
    elec_import_t2: float = Field(default=0, ge=-5, le=5)
    elec_export_t1: float = Field(default=0, ge=-5, le=5)
    elec_export_t2: float = Field(default=0, ge=-5, le=5)
    netting: bool = True
    gas_price: float = Field(default=0, ge=-10, le=20)
    gas_dynamic: bool = False
    vat_pct: float = Field(default=21, ge=0, le=50)
    fixed_monthly_elec: float = Field(default=0, ge=-1000, le=1000)
    fixed_monthly_gas: float = Field(default=0, ge=-1000, le=1000)
    energy_tax_rebate_year: float = Field(default=0, ge=0, le=5000)
    advance_monthly: float = Field(default=0, ge=0, le=10000)


CONTRACT_COLS = list(ContractIn.model_fields)


@router.get("/contracts", tags=["costs"])
async def list_contracts(request: Request):
    return [_row(r) for r in await request.app.state.pool.fetch("SELECT * FROM tariff_contract ORDER BY valid_from DESC")]


@router.post("/contracts", tags=["costs"], status_code=201, dependencies=[Depends(require_admin)])
async def create_contract(body: ContractIn, request: Request):
    _check_dates(body)
    cols = ", ".join(CONTRACT_COLS)
    vals = ", ".join(f"${i + 1}" for i in range(len(CONTRACT_COLS)))
    r = await request.app.state.pool.fetchrow(f"INSERT INTO tariff_contract ({cols}) VALUES ({vals}) RETURNING *",
                                              *[getattr(body, c) for c in CONTRACT_COLS])
    return _row(r)


@router.put("/contracts/{cid}", tags=["costs"], dependencies=[Depends(require_admin)])
async def update_contract(cid: int, body: ContractIn, request: Request):
    _check_dates(body)
    sets = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(CONTRACT_COLS))
    r = await request.app.state.pool.fetchrow(f"UPDATE tariff_contract SET {sets} WHERE id = $1 RETURNING *",
                                              cid, *[getattr(body, c) for c in CONTRACT_COLS])
    if not r:
        raise HTTPException(404, "contract not found")
    return _row(r)


@router.delete("/contracts/{cid}", tags=["costs"], status_code=204, dependencies=[Depends(require_admin)])
async def delete_contract(cid: int, request: Request):
    await request.app.state.pool.execute("DELETE FROM tariff_contract WHERE id = $1", cid)


def _check_dates(body: ContractIn):
    if body.valid_to and body.valid_to <= body.valid_from:
        raise HTTPException(422, "valid_to must be after valid_from")


# --- costs -------------------------------------------------------------------

def _cost_out(r: dict) -> dict:
    return {k: _j(v) for k, v in r.items() if k not in ("day", "netting")}


@router.get("/costs", tags=["costs"], summary="Costs per day, month or year (F-COST-04)")
async def get_costs(request: Request, start: datetime, end: datetime,
                    resolution: Literal["1d", "1mo", "1y"] = "1d"):
    start, end = _range(start, end)
    pool = request.app.state.pool
    rows = await costs.cost_rows(pool, start, end)
    contracts = await costs.load_contracts(pool)
    summary = costs.summarize(rows, contracts)
    rows = costs.allocate_netting(rows, summary["netting_correction_eur"])
    return {"resolution": resolution, "rows": [_cost_out(r) for r in costs.group(rows, resolution)],
            "summary": summary, "has_contract": bool(contracts.items)}


@router.get("/costs/today", tags=["costs"])
async def costs_today(request: Request):
    now = datetime.now(ZONE)
    start = datetime(now.year, now.month, now.day, tzinfo=ZONE)
    pool = request.app.state.pool
    rows = await costs.cost_rows(pool, start, start + timedelta(days=1))
    contracts = await costs.load_contracts(pool)
    return {**costs.summarize(rows, contracts), "has_contract": contracts.on(now.date()) is not None}


async def _avg_spot(pool, kind: str, now: datetime) -> float | None:
    """Average market price over the last year, as the spot estimate for future dynamic days."""
    return await pool.fetchval("SELECT avg(price) FROM price WHERE kind = $1 AND ts > $2::timestamptz - interval '365 days' "
                               "AND ts <= $2", kind, now)


@router.get("/costs/projection", tags=["costs"], summary="Projected year-end total vs advance payments")
async def projection(request: Request, year: int | None = None):
    pool = request.app.state.pool
    now = datetime.now(ZONE)
    year = year or now.year
    y0, y1 = datetime(year, 1, 1, tzinfo=ZONE), datetime(year + 1, 1, 1, tzinfo=ZONE)
    contracts = await costs.load_contracts(pool)
    ytd_rows = await costs.cost_rows(pool, y0, min(y1, now))
    ytd = costs.summarize(ytd_rows, contracts)
    days_done = len([r for r in ytd_rows if r["total_eur"] is not None])
    days_in_year = (y1 - y0).days
    remaining = max(0, days_in_year - (min(now.date(), y1.date()) - y0.date()).days)
    # Estimate the rest of the year from the same dates last year when known,
    # otherwise from the average day so far.
    ly = await costs.cost_rows(pool, datetime(year - 1, now.month, now.day, tzinfo=ZONE),
                               datetime(year, 1, 1, tzinfo=ZONE)) if remaining else []
    ly_priced = [r for r in ly if r["import_kwh"] is not None]
    method = "average_so_far"
    rest = 0.0
    c = contracts.on(now.date())
    elec_spot = gas_spot = None
    if c and c.kind == "dynamic":
        elec_spot = await _avg_spot(pool, "elec", now)
    if c and c.gas_dynamic:
        gas_spot = await _avg_spot(pool, "gas", now)
    spot_ok = c is not None and (c.kind != "dynamic" or elec_spot is not None) and \
        (not c.gas_dynamic or gas_spot is not None)
    if remaining and c and spot_ok and len(ly_priced) >= remaining * 0.8:
        method = "same_period_last_year"
        for r in ly_priced:
            row = {"import_t1_kwh": r["import_kwh"], "import_t2_kwh": 0, "export_t1_kwh": r["export_kwh"],
                   "export_t2_kwh": 0, "gas_m3": r["gas_m3"]}
            p = costs.price_bucket(row, c, elec_spot, gas_spot)
            rest += p["import_eur"] - p["export_eur"] + p["gas_eur"] + c.fixed_per_day(r["day"])
    elif remaining and days_done:
        rest = ytd["total_eur"] / days_done * remaining
    advance = (c.advance_monthly * 12) if c else 0
    projected = round(ytd["total_eur"] + rest, 2)
    return {"year": year, "ytd_eur": ytd["total_eur"], "projected_eur": projected, "method": method,
            "advance_year_eur": round(advance, 2),
            "difference_eur": round(advance - projected, 2) if advance else None,
            "days_with_data": days_done}


# --- prices ------------------------------------------------------------------

@router.get("/prices", tags=["costs"], summary="Day-ahead prices with the all-in price of the current contract")
async def get_prices(request: Request, start: datetime, end: datetime, kind: Literal["elec", "gas"] = "elec"):
    start, end = _range(start, end)
    pool = request.app.state.pool
    contracts = await costs.load_contracts(pool)
    rows = await pool.fetch("SELECT ts, price, duration FROM price WHERE kind = $1 AND ts >= $2 AND ts < $3 "
                            "ORDER BY ts", kind, start, end)
    out = []
    for r in rows:
        c = contracts.on(r["ts"].astimezone(ZONE).date())
        allin = None
        if c:
            allin = c.import_price(1, r["price"]) if kind == "elec" else c.gas(r["price"])
            if (kind == "elec" and c.kind != "dynamic") or (kind == "gas" and not c.gas_dynamic):
                allin = None  # contract price does not follow the market
        out.append({"ts": r["ts"].isoformat(), "spot": r["price"], "all_in": allin,
                    "minutes": int(r["duration"].total_seconds() // 60)})
    return {"kind": kind, "rows": out}


@router.get("/prices/cheapest", tags=["costs"], summary="Cheapest upcoming block for flexible loads (F-COST-05)")
async def cheapest(request: Request, hours: float = Query(2, gt=0, le=12)):
    now = datetime.now(timezone.utc)
    rows = await request.app.state.pool.fetch(
        "SELECT ts, price, duration FROM price WHERE kind = 'elec' AND ts >= $1 ORDER BY ts", now)
    if not rows:
        return {"available": False}
    step = rows[0]["duration"]
    pts = [(r["ts"], r["price"]) for r in rows if r["duration"] == step]
    best = costs.cheapest_block(pts, hours, step)
    cheapest_hours = sorted(pts, key=lambda p: p[1])[: max(1, round(timedelta(hours=hours) / step))]
    return {"available": True, "until": (rows[-1]["ts"] + rows[-1]["duration"]).isoformat(),
            "block": {k: _j(v) for k, v in best.items()} if best else None,
            "cheapest": [{"ts": t.isoformat(), "spot": p} for t, p in sorted(cheapest_hours)]}


# --- base load ---------------------------------------------------------------

@router.get("/baseload", tags=["baseload"], summary="Base load per day, trend, step changes (F-BASE)")
async def get_baseload(request: Request, days: int = Query(365, ge=7, le=3650)):
    pool = request.app.state.pool
    rows = await pool.fetch("SELECT day, watts, confidence FROM baseload_1d WHERE day > current_date - $1::int "
                            "ORDER BY day", days)
    series = [(r["day"], r["watts"]) for r in rows]
    recent = [w for d, w in series[-7:] if w is not None]
    current = median(recent) if recent else None
    contracts = await costs.load_contracts(pool)
    c = contracts.on(datetime.now(ZONE).date())
    price = (c.import_price(1, None) + c.import_price(2, None)) / 2 if c and c.kind != "dynamic" else None
    if c and c.kind == "dynamic":
        avg_spot = await pool.fetchval("SELECT avg(price) FROM price WHERE kind = 'elec' AND ts > now() - interval '30 days'")
        price = c.import_price(1, avg_spot or 0)
    kwh_year = current * 8.766 if current is not None else None  # W * 8766 h / 1000
    total = await pool.fetchval(
        "SELECT sum(import_t1_kwh + import_t2_kwh) FROM energy_1d WHERE bucket > now() - interval '28 days'")
    share = (current * 24 * 28 / 1000) / total if current and total else None
    return {
        "current_w": current, "kwh_year": kwh_year, "eur_year": kwh_year * price if kwh_year and price else None,
        "share_of_import": share, "method": baseload.METHOD,
        "rows": [{"day": d.isoformat(), "watts": w, "confidence": r["confidence"]} for (d, w), r in zip(series, rows)],
        "steps": [{k: _j(v) for k, v in s.items()} for s in baseload.step_changes(series)],
    }


# --- alert rules -------------------------------------------------------------

class RuleIn(BaseModel):
    kind: str
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    params: dict = {}
    channel_ids: list[int] = []
    quiet_start: time | None = None
    quiet_end: time | None = None
    cooldown_min: int = Field(default=60, ge=0, le=10080)

    @field_validator("kind")
    @classmethod
    def known(cls, v):
        if v not in alerts.RULE_KINDS:
            raise ValueError(f"unknown rule kind; choose from {', '.join(alerts.RULE_KINDS)}")
        return v


@router.get("/alerts/kinds", tags=["alerts"])
async def rule_kinds():
    return alerts.RULE_KINDS


@router.get("/alerts/rules", tags=["alerts"])
async def list_rules(request: Request):
    return [_row(r) for r in await request.app.state.pool.fetch("SELECT * FROM alert_rule ORDER BY id")]


@router.post("/alerts/rules", tags=["alerts"], status_code=201, dependencies=[Depends(require_admin)])
async def create_rule(body: RuleIn, request: Request):
    r = await request.app.state.pool.fetchrow(
        "INSERT INTO alert_rule (kind, name, enabled, params, channel_ids, quiet_start, quiet_end, cooldown_min) "
        "VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8) RETURNING *",
        body.kind, body.name, body.enabled, json.dumps({**alerts.RULE_KINDS[body.kind], **body.params}),
        body.channel_ids, body.quiet_start, body.quiet_end, body.cooldown_min)
    return _row(r)


@router.put("/alerts/rules/{rid}", tags=["alerts"], dependencies=[Depends(require_admin)])
async def update_rule(rid: int, body: RuleIn, request: Request):
    r = await request.app.state.pool.fetchrow(
        "UPDATE alert_rule SET kind = $2, name = $3, enabled = $4, params = $5::jsonb, channel_ids = $6, "
        "quiet_start = $7, quiet_end = $8, cooldown_min = $9 WHERE id = $1 RETURNING *",
        rid, body.kind, body.name, body.enabled, json.dumps({**alerts.RULE_KINDS[body.kind], **body.params}),
        body.channel_ids, body.quiet_start, body.quiet_end, body.cooldown_min)
    if not r:
        raise HTTPException(404, "rule not found")
    return _row(r)


@router.delete("/alerts/rules/{rid}", tags=["alerts"], status_code=204, dependencies=[Depends(require_admin)])
async def delete_rule(rid: int, request: Request):
    await request.app.state.pool.execute("DELETE FROM alert_rule WHERE id = $1", rid)


class Snooze(BaseModel):
    minutes: int = Field(ge=0, le=60 * 24 * 30)


@router.post("/alerts/rules/{rid}/snooze", tags=["alerts"])
async def snooze(rid: int, body: Snooze, request: Request):
    until = datetime.now(timezone.utc) + timedelta(minutes=body.minutes) if body.minutes else None
    r = await request.app.state.pool.fetchrow("UPDATE alert_rule SET snooze_until = $2 WHERE id = $1 RETURNING *",
                                              rid, until)
    if not r:
        raise HTTPException(404, "rule not found")
    return _row(r)


# --- events ------------------------------------------------------------------

@router.get("/events", tags=["alerts"])
async def list_events(request: Request, kind: str | None = None, limit: int = Query(100, ge=1, le=1000),
                      before_id: int | None = None):
    rows = await request.app.state.pool.fetch(
        "SELECT e.*, r.name AS rule_name FROM event e LEFT JOIN alert_rule r ON r.id = e.rule_id "
        "WHERE ($1::text IS NULL OR e.kind = $1) AND ($2::bigint IS NULL OR e.id < $2) ORDER BY e.id DESC LIMIT $3",
        kind, before_id, limit)
    return [_row(r) for r in rows]


@router.post("/events/{eid}/ack", tags=["alerts"], status_code=204)
async def ack(eid: int, request: Request):
    await request.app.state.pool.execute("UPDATE event SET acknowledged = true WHERE id = $1", eid)


# --- channels ----------------------------------------------------------------

class ChannelIn(BaseModel):
    kind: Literal["webpush", "email", "ntfy", "telegram", "homeassistant", "webhook"]
    name: str = Field(min_length=1, max_length=100)
    config: dict = {}
    enabled: bool = True


def _channel_out(r) -> dict:
    d = _row(r)
    d["config"] = notify.redact(d["kind"], d["config"])
    return d


@router.get("/channels", tags=["alerts"])
async def list_channels(request: Request):
    return [_channel_out(r) for r in await request.app.state.pool.fetch("SELECT * FROM channel ORDER BY id")]


@router.get("/channels/fields", tags=["alerts"])
async def channel_fields():
    return notify.CHANNEL_FIELDS


@router.post("/channels", tags=["alerts"], status_code=201, dependencies=[Depends(require_admin)])
async def create_channel(body: ChannelIn, request: Request):
    cfg = {k: v for k, v in body.config.items() if k in notify.CHANNEL_FIELDS[body.kind]}
    r = await request.app.state.pool.fetchrow(
        "INSERT INTO channel (kind, name, config, enabled) VALUES ($1, $2, $3::jsonb, $4) RETURNING *",
        body.kind, body.name, json.dumps(cfg), body.enabled)
    return _channel_out(r)


@router.put("/channels/{cid}", tags=["alerts"], dependencies=[Depends(require_admin)])
async def update_channel(cid: int, body: ChannelIn, request: Request):
    pool = request.app.state.pool
    old = await pool.fetchrow("SELECT * FROM channel WHERE id = $1", cid)
    if not old:
        raise HTTPException(404, "channel not found")
    old_cfg = _row(old)["config"]
    cfg = {}
    for k in notify.CHANNEL_FIELDS[body.kind]:
        v = body.config.get(k)
        cfg[k] = old_cfg.get(k) if v == "••••" else v  # keep secrets the UI only saw redacted
    r = await pool.fetchrow("UPDATE channel SET kind = $2, name = $3, config = $4::jsonb, enabled = $5 "
                            "WHERE id = $1 RETURNING *", cid, body.kind, body.name,
                            json.dumps({k: v for k, v in cfg.items() if v not in (None, "")}), body.enabled)
    return _channel_out(r)


@router.delete("/channels/{cid}", tags=["alerts"], status_code=204, dependencies=[Depends(require_admin)])
async def delete_channel(cid: int, request: Request):
    await request.app.state.pool.execute("DELETE FROM channel WHERE id = $1", cid)


@router.post("/channels/{cid}/test", tags=["alerts"], dependencies=[Depends(require_admin)])
async def test_channel(cid: int, request: Request):
    pool = request.app.state.pool
    ch = await pool.fetchrow("SELECT * FROM channel WHERE id = $1", cid)
    if not ch:
        raise HTTPException(404, "channel not found")
    async with httpx.AsyncClient() as client:
        try:
            await notify.send(pool, client, _row(ch), "P1 Monitor test", "This channel works.")
        except Exception as e:
            cfg = ch["config"]
            raise HTTPException(502, notify.safe_error(e, json.loads(cfg) if isinstance(cfg, str) else dict(cfg or {})))
    return {"ok": True}


# --- web push ----------------------------------------------------------------

class PushSub(BaseModel):
    endpoint: str = Field(pattern=r"^https://")
    keys: dict


@router.get("/push/key", tags=["alerts"])
async def push_key(request: Request):
    return {"public_key": (await notify.vapid_keys(request.app.state.pool))["public"]}


@router.post("/push/subscribe", tags=["alerts"], status_code=204)
async def push_subscribe(body: PushSub, request: Request, user: User = Depends(current_user)):
    pool = request.app.state.pool
    await pool.execute("INSERT INTO push_subscription (user_id, endpoint, keys) VALUES ($1, $2, $3::jsonb) "
                       "ON CONFLICT (endpoint) DO UPDATE SET keys = EXCLUDED.keys, user_id = EXCLUDED.user_id",
                       user.id, body.endpoint, json.dumps(body.keys))
    # make sure there is a web push channel to route alerts to
    if not await pool.fetchval("SELECT EXISTS (SELECT 1 FROM channel WHERE kind = 'webpush')"):
        await pool.execute("INSERT INTO channel (kind, name) VALUES ('webpush', 'Web push')")


@router.post("/push/unsubscribe", tags=["alerts"], status_code=204)
async def push_unsubscribe(body: PushSub, request: Request):
    await request.app.state.pool.execute("DELETE FROM push_subscription WHERE endpoint = $1", body.endpoint)


@router.get("/jobs", tags=["status"])
async def jobs(request: Request):
    return [_row(r) for r in await request.app.state.pool.fetch("SELECT * FROM job_status ORDER BY name")]

