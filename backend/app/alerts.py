"""Alert rules (F-ALERT-01..04, 06): evaluation, de-duplication, quiet hours,
snooze, and dispatch to the chosen notification channels."""
from __future__ import annotations

import json
import logging
from datetime import datetime, time, timedelta, timezone
from statistics import mean, median, pstdev

from . import notify
from .costs import ZONE

log = logging.getLogger(__name__)

RULE_KINDS = {
    "anomaly_hour": {"sigma": 4, "weeks": 8, "min_kwh": 0.3},
    "anomaly_day": {"sigma": 3, "weeks": 8, "min_kwh": 1},
    "baseload_increase": {"watts": 30, "days": 3},
    "power_above": {"watts": 3000, "minutes": 10},
    "daily_kwh_above": {"kwh": 15},
    "gas_while_away": {"from": "09:00", "to": "17:00", "m3": 0.05, "weekdays": [0, 1, 2, 3, 4]},
    "meter_offline": {"minutes": 2},
    "power_failure": {},
    "voltage_out_of_range": {"min": 207, "max": 253},
}


class Fire:
    def __init__(self, message: str, data: dict | None = None, severity: str = "warning"):
        self.message, self.data, self.severity = message, data or {}, severity


def _jl(v):
    return json.loads(v) if isinstance(v, str) else (v or {})


def anomaly(value: float, history: list[float], sigma: float, min_value: float) -> dict | None:
    """value is unusual if above mean + sigma * std of comparable periods."""
    if len(history) < 4 or value < min_value:
        return None
    mu = mean(history)
    sd = max(pstdev(history), 0.1 * mu, 0.05)
    limit = mu + sigma * sd
    if value > limit:
        return {"value": round(value, 3), "mean": round(mu, 3), "std": round(sd, 3), "limit": round(limit, 3)}
    return None


async def _import_kwh(pool, view: str, start: datetime) -> float | None:
    v = await pool.fetchval(f"SELECT import_t1_kwh + import_t2_kwh FROM {view} WHERE bucket = $1", start)
    return v


async def eval_anomaly(pool, rule, period: str, now: datetime) -> Fire | None:
    p = {**RULE_KINDS[f"anomaly_{period}"], **_jl(rule["params"])}
    state = _jl(rule["state"])
    local = now.astimezone(ZONE)
    if period == "hour":
        start = (now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)).astimezone(ZONE)
        view, step = "energy_1h", timedelta(days=7)
    else:
        y = local.date() - timedelta(days=1)
        start = datetime(y.year, y.month, y.day, tzinfo=ZONE)
        view, step = "energy_1d", timedelta(days=7)
    key = start.isoformat()
    if state.get("last") == key:
        return None
    value = await _import_kwh(pool, view, start)
    hist = []
    for w in range(1, int(p["weeks"]) + 1):
        ref = (start.replace(tzinfo=None) - step * w).replace(tzinfo=ZONE)
        v = await _import_kwh(pool, view, ref)
        if v is not None:
            hist.append(v)
    await pool.execute("UPDATE alert_rule SET state = state || $2::jsonb WHERE id = $1", rule["id"],
                       json.dumps({"last": key}))
    if value is None:
        return None
    a = anomaly(value, hist, float(p["sigma"]), float(p["min_kwh"]))
    if not a:
        return None
    when = start.strftime("%a %H:00") if period == "hour" else start.strftime("%a %d %b")
    unit = "this hour" if period == "hour" else "that day"
    return Fire(f"Unusual consumption {when}: {a['value']:.2f} kWh, usually {a['mean']:.2f} kWh {unit} "
                f"(limit {a['limit']:.2f}).", {"period": period, "start": key, **a})


async def eval_baseload_increase(pool, rule, now: datetime) -> Fire | None:
    p = {**RULE_KINDS["baseload_increase"], **_jl(rule["params"])}
    days = int(p["days"])
    rows = await pool.fetch("SELECT day, watts FROM baseload_1d WHERE watts IS NOT NULL AND confidence > 0.8 "
                            "ORDER BY day DESC LIMIT $1", days + 28)
    if len(rows) < days + 7:
        return None
    recent = [r["watts"] for r in rows[:days]]
    ref = median(r["watts"] for r in rows[days:])
    last_day = rows[0]["day"].isoformat()
    state = _jl(rule["state"])
    if state.get("last") == last_day:
        return None
    await pool.execute("UPDATE alert_rule SET state = state || $2::jsonb WHERE id = $1", rule["id"],
                       json.dumps({"last": last_day}))
    if min(recent) >= ref + float(p["watts"]):
        extra = min(recent) - ref
        return Fire(f"Base load rose from about {ref:.0f} W to {min(recent):.0f} W for {days} days "
                    f"(+{extra:.0f} W, about {extra * 8.766:.0f} kWh/year).",
                    {"reference_w": ref, "recent_w": recent})
    return None


async def eval_power_above(pool, rule, now: datetime) -> Fire | None:
    p = {**RULE_KINDS["power_above"], **_jl(rule["params"])}
    minutes = int(p["minutes"])
    rows = await pool.fetch("SELECT power_avg_w FROM reading_1m WHERE bucket >= $1 AND bucket < $2",
                            now - timedelta(minutes=minutes + 1), now - timedelta(minutes=1))
    if len(rows) >= max(1, int(minutes * 0.9)) and all((r["power_avg_w"] or 0) > float(p["watts"]) for r in rows):
        avg = mean(r["power_avg_w"] for r in rows)
        return Fire(f"Power above {float(p['watts']):.0f} W for {minutes} min (average {avg:.0f} W).",
                    {"avg_w": avg})
    return None


async def eval_daily_kwh(pool, rule, now: datetime) -> Fire | None:
    p = {**RULE_KINDS["daily_kwh_above"], **_jl(rule["params"])}
    local = now.astimezone(ZONE)
    today = local.date().isoformat()
    if _jl(rule["state"]).get("day") == today:
        return None
    start = datetime(local.year, local.month, local.day, tzinfo=ZONE)
    v = await pool.fetchval("SELECT sum(import_t1_kwh + import_t2_kwh) FROM energy_15m WHERE bucket >= $1", start)
    if v is not None and v > float(p["kwh"]):
        await pool.execute("UPDATE alert_rule SET state = state || $2::jsonb WHERE id = $1", rule["id"],
                           json.dumps({"day": today}))
        return Fire(f"Today's consumption passed {float(p['kwh']):.1f} kWh ({v:.1f} kWh so far).", {"kwh": v})
    return None


async def eval_gas_while_away(pool, rule, now: datetime) -> Fire | None:
    p = {**RULE_KINDS["gas_while_away"], **_jl(rule["params"])}
    local = now.astimezone(ZONE)
    if local.weekday() not in p.get("weekdays", list(range(7))):
        return None
    t_from, t_to = time.fromisoformat(p["from"]), time.fromisoformat(p["to"])
    if not (t_from <= local.time() < t_to):
        return None
    today = local.date().isoformat()
    if _jl(rule["state"]).get("day") == today:
        return None
    start = datetime.combine(local.date(), t_from, ZONE)
    used = await pool.fetchval(
        "SELECT max(gas_m3) - min(gas_m3) FROM reading_1m WHERE bucket >= $1 AND bucket <= $2", start, now)
    if used is not None and used > float(p["m3"]):
        await pool.execute("UPDATE alert_rule SET state = state || $2::jsonb WHERE id = $1", rule["id"],
                           json.dumps({"day": today}))
        return Fire(f"Gas used while away: {used:.3f} m³ since {p['from']}.", {"m3": used})
    return None


async def eval_meter_offline(pool, rule, now: datetime) -> Fire | None:
    p = {**RULE_KINDS["meter_offline"], **_jl(rule["params"])}
    row = await pool.fetchrow("SELECT last_reading_at, updated_at, last_error FROM collector_status WHERE id = 1")
    last = row["last_reading_at"] if row else None
    if last is None:
        return None
    age = (now - last).total_seconds() / 60
    if age > float(p["minutes"]):
        return Fire(f"No data from the P1 meter for {age:.0f} minutes. {row['last_error'] or ''}".strip(),
                    {"minutes": age}, "error")
    return None


async def eval_power_failure(pool, rule, now: datetime) -> Fire | None:
    state = _jl(rule["state"])
    last_id = state.get("event_id")
    if last_id is None:  # don't replay history on first run
        last_id = await pool.fetchval("SELECT coalesce(max(id), 0) FROM event WHERE kind = 'power_failure'")
        await pool.execute("UPDATE alert_rule SET state = state || $2::jsonb WHERE id = $1", rule["id"],
                           json.dumps({"event_id": last_id}))
        return None
    ev = await pool.fetchrow("SELECT id, ts, data FROM event WHERE kind = 'power_failure' AND id > $1 "
                             "ORDER BY id DESC LIMIT 1", last_id)
    if not ev:
        return None
    await pool.execute("UPDATE alert_rule SET state = state || $2::jsonb WHERE id = $1", rule["id"],
                       json.dumps({"event_id": ev["id"]}))
    return Fire(f"Power failure detected at {ev['ts'].astimezone(ZONE):%H:%M}.", _jl(ev["data"]), "error")


async def eval_voltage(pool, rule, now: datetime) -> Fire | None:
    p = {**RULE_KINDS["voltage_out_of_range"], **_jl(rule["params"])}
    row = await pool.fetchrow("SELECT min(voltage_min_v) AS lo, max(voltage_max_v) AS hi FROM reading_1m "
                              "WHERE bucket >= $1", now - timedelta(minutes=3))
    if not row or row["lo"] is None:
        return None
    if row["lo"] < float(p["min"]) or row["hi"] > float(p["max"]):
        return Fire(f"Voltage out of range: {row['lo']:.1f}–{row['hi']:.1f} V "
                    f"(allowed {float(p['min']):.0f}–{float(p['max']):.0f} V).", {"min": row["lo"], "max": row["hi"]})
    return None


EVALUATORS = {
    "anomaly_hour": lambda pool, r, now: eval_anomaly(pool, r, "hour", now),
    "anomaly_day": lambda pool, r, now: eval_anomaly(pool, r, "day", now),
    "baseload_increase": eval_baseload_increase,
    "power_above": eval_power_above,
    "daily_kwh_above": eval_daily_kwh,
    "gas_while_away": eval_gas_while_away,
    "meter_offline": eval_meter_offline,
    "power_failure": eval_power_failure,
    "voltage_out_of_range": eval_voltage,
}


def in_quiet_hours(rule, now: datetime) -> bool:
    qs, qe = rule["quiet_start"], rule["quiet_end"]
    if qs is None or qe is None or qs == qe:
        return False
    t = now.astimezone(ZONE).time()
    return qs <= t < qe if qs < qe else (t >= qs or t < qe)


async def deliver(pool, client, rule, fire: Fire, now: datetime) -> None:
    """Record the alert as an event and notify, honouring cooldown, snooze and quiet hours."""
    if rule["last_fired_at"] and rule["cooldown_min"] and \
            now - rule["last_fired_at"] < timedelta(minutes=rule["cooldown_min"]):
        return  # de-duplicated
    await pool.execute("UPDATE alert_rule SET last_fired_at = $2 WHERE id = $1", rule["id"], now)
    title = f"P1: {rule['name']}"
    result = None
    if rule["snooze_until"] and rule["snooze_until"] > now:
        result = "snoozed"
    elif in_quiet_hours(rule, now):
        result = "quiet hours"
    ev_id = await pool.fetchval(
        "INSERT INTO event (ts, kind, severity, message, data, rule_id, notify_result) "
        "VALUES ($1, 'alert', $2, $3, $4::jsonb, $5, $6) RETURNING id",
        now, fire.severity, fire.message, json.dumps({"rule_kind": rule["kind"], **fire.data}, default=str),
        rule["id"], result)
    if result:
        return
    await dispatch(pool, client, rule["channel_ids"], title, fire.message, {"event_id": ev_id}, ev_id)


def _cfg(ch) -> dict:
    cfg = ch["config"]
    return json.loads(cfg) if isinstance(cfg, str) else dict(cfg or {})


async def dispatch(pool, client, channel_ids, title, body, data=None, event_id=None) -> list[str]:
    if channel_ids:
        channels = await pool.fetch("SELECT * FROM channel WHERE enabled AND id = ANY($1::bigint[])", channel_ids)
    else:
        channels = await pool.fetch("SELECT * FROM channel WHERE enabled")
    results = []
    for ch in channels:
        try:
            await notify.send(pool, client, dict(ch), title, body, data)
            results.append(f"{ch['name']}: ok")
        except Exception as e:  # one failing channel must not block the others
            err = notify.safe_error(e, _cfg(ch))
            log.warning("channel %s failed: %s", ch["name"], err)
            results.append(f"{ch['name']}: {err}")
    if event_id is not None:
        await pool.execute("UPDATE event SET notified_at = now(), notify_result = $2 WHERE id = $1", event_id,
                           "; ".join(results) or "no channels configured")
    return results


async def run_rules(pool, client, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    fired = 0
    for rule in await pool.fetch("SELECT * FROM alert_rule WHERE enabled ORDER BY id"):
        ev = EVALUATORS.get(rule["kind"])
        if ev is None:
            continue
        try:
            fire = await ev(pool, rule, now)
        except Exception:
            log.exception("rule %s failed", rule["name"])
            continue
        if fire:
            rule = await pool.fetchrow("SELECT * FROM alert_rule WHERE id = $1", rule["id"])
            await deliver(pool, client, rule, fire, now)
            fired += 1
    return fired
