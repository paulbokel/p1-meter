"""Automations (F-AUTO-01/02): switch a device on solar surplus, low prices or
in the planned cheapest hours, through MQTT, a Home Assistant service call or
a webhook. Evaluated by the worker every 30 s."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, time, timedelta, timezone

import aiomqtt
import httpx

from . import costs
from .costs import ZONE
from .notify import safe_error

log = logging.getLogger(__name__)

TRIGGERS = {
    "surplus": {"watts": 1500, "minutes": 5},
    "price_below": {"eur_kwh": 0.10, "basis": "all_in"},
    "cheapest_hours": {"hours": 3, "from": "00:00", "to": "00:00", "contiguous": False},
}
ACTION_FIELDS = {
    "mqtt": ["topic", "payload", "retain", "host"],
    "homeassistant": ["url", "token", "service", "data"],
    "webhook": ["url", "method", "body", "secret"],
}
SECRETS = {"token", "secret"}
MASK = "••••"


def _j(v):
    return json.loads(v) if isinstance(v, str) else (v if v is not None else {})


def redact_action(a: dict | None) -> dict | None:
    if not a:
        return a
    return {k: (MASK if k in SECRETS and v else v) for k, v in a.items()}


def keep_secrets(new: dict | None, old: dict | None) -> dict | None:
    """A redacted secret sent back from the UI keeps the stored value."""
    if not new:
        return new
    old = old or {}
    return {k: (old.get(k) if k in SECRETS and v == MASK else v) for k, v in new.items()}


# --- trigger evaluation --------------------------------------------------------

async def eval_surplus(pool, p: dict, now: datetime) -> bool | None:
    """True when export >= watts in every minute of the window, False when below
    in every minute, None (keep state) in between or with too little data."""
    minutes = max(1, int(p.get("minutes", 5)))
    end = now.replace(second=0, microsecond=0)
    rows = await pool.fetch("SELECT power_avg_w FROM reading_1m WHERE bucket >= $1 AND bucket < $2",
                            end - timedelta(minutes=minutes), end)
    vals = [r["power_avg_w"] for r in rows if r["power_avg_w"] is not None]
    if len(vals) < max(1, int(minutes * 0.8)):
        return None
    watts = float(p.get("watts", 1500))
    if all(-v >= watts for v in vals):
        return True
    if all(-v < watts for v in vals):
        return False
    return None


async def current_price(pool, now: datetime, basis: str) -> float | None:
    row = await pool.fetchrow("SELECT price FROM price WHERE kind = 'elec' AND ts <= $1 AND ts + duration > $1 "
                              "ORDER BY ts DESC LIMIT 1", now)
    if row is None:
        return None
    spot = row["price"]
    if basis == "all_in":
        c = (await costs.load_contracts(pool)).on(now.astimezone(ZONE).date())
        if c and c.kind == "dynamic":
            return c.import_price(1, spot)
    return spot


async def eval_price_below(pool, p: dict, now: datetime) -> bool:
    price = await current_price(pool, now, p.get("basis", "all_in"))
    return price is not None and price < float(p.get("eur_kwh", 0.1))


def _hm(s: str) -> time:
    h, m = (int(x) for x in str(s).split(":"))
    return time(h % 24, m)


def window_for(now: datetime, frm: str, to: str) -> tuple[datetime, datetime]:
    """The local window [from, to) that contains now, or else the next one.
    `to` <= `from` means the window runs past midnight; equal means 24 h."""
    local = now.astimezone(ZONE)
    f, t = _hm(frm), _hm(to)

    def at(d, tm):
        return datetime.combine(d, tm, tzinfo=ZONE)

    for day in (local.date() - timedelta(days=1), local.date(), local.date() + timedelta(days=1)):
        start = at(day, f)
        end_day = day if t > f else day + timedelta(days=1)
        end = at(end_day, t)
        if now < end.astimezone(timezone.utc):
            return start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    raise AssertionError("unreachable")


async def make_plan(pool, p: dict, start: datetime, end: datetime) -> dict | None:
    rows = await pool.fetch("SELECT ts, price, duration FROM price WHERE kind = 'elec' AND ts >= $1 AND ts < $2 "
                            "ORDER BY ts", start, end)
    if not rows or rows[-1]["ts"] + rows[-1]["duration"] < end:
        return None  # prices for the whole window are not published yet
    plan = costs.plan_cheapest([(r["ts"], r["price"], r["duration"]) for r in rows], float(p.get("hours", 3)),
                               bool(p.get("contiguous")))
    if not plan:
        return None
    return {"window": [start.isoformat(), end.isoformat()], "avg_price": plan["avg_price"],
            "slots": [[s.isoformat(), e.isoformat()] for s, e in plan["slots"]]}


async def eval_cheapest(pool, a: dict, p: dict, now: datetime) -> bool:
    start, end = window_for(now, p.get("from", "00:00"), p.get("to", "00:00"))
    state = _j(a["state"])
    plan = state.get("plan")
    if not plan or plan.get("window") != [start.isoformat(), end.isoformat()]:
        plan = await make_plan(pool, p, start, end)
        if plan:
            await pool.execute("UPDATE automation SET state = state || $2::jsonb WHERE id = $1", a["id"],
                               json.dumps({"plan": plan}))
            a["state"] = {**state, "plan": plan}
    if not plan:
        return False
    return any(datetime.fromisoformat(s) <= now < datetime.fromisoformat(e) for s, e in plan["slots"])


async def desired(pool, a: dict, now: datetime) -> bool | None:
    p = {**TRIGGERS[a["trigger_kind"]], **_j(a["trigger_params"])}
    if a["trigger_kind"] == "surplus":
        return await eval_surplus(pool, p, now)
    if a["trigger_kind"] == "price_below":
        return await eval_price_below(pool, p, now)
    return await eval_cheapest(pool, a, p, now)


# --- actions --------------------------------------------------------------------

async def run_action(client: httpx.AsyncClient, action: dict, name: str, on: bool, env=os.environ) -> None:
    kind = action.get("kind")
    if kind == "mqtt":
        host = action.get("host") or env.get("MQTT_HOST")
        if not host:
            raise ValueError("no MQTT broker: set MQTT_HOST or a host in the action")
        async with aiomqtt.Client(host, int(env.get("MQTT_PORT", "1883")), username=env.get("MQTT_USER") or None,
                                  password=env.get("MQTT_PASSWORD") or None, timeout=10) as mq:
            payload = action.get("payload", "ON" if on else "OFF")
            await mq.publish(action["topic"], payload if isinstance(payload, str) else json.dumps(payload),
                             retain=bool(action.get("retain")))
    elif kind == "homeassistant":
        domain, _, service = action["service"].partition(".")
        url = action["url"].rstrip("/") + f"/api/services/{domain}/{service}"
        r = await client.post(url, json=action.get("data") or {}, timeout=15,
                              headers={"Authorization": f"Bearer {action['token']}"})
        r.raise_for_status()
    elif kind == "webhook":
        headers = {"X-P1-Secret": action["secret"]} if action.get("secret") else {}
        body = action.get("body") or {"automation": name, "active": on}
        method = (action.get("method") or "POST").upper()
        r = await client.request(method, action["url"], json=None if method == "GET" else body, headers=headers,
                                 timeout=15)
        r.raise_for_status()
    else:
        raise ValueError(f"unknown action kind {kind!r}")


async def switch(pool, client, a: dict, on: bool, now: datetime) -> bool:
    """Run the on/off action and store the new state. False when the action failed
    (the state stays, so the next evaluation retries)."""
    action = _j(a["action"]) if on else (_j(a["off_action"]) if a["off_action"] else None)
    try:
        if action:
            await run_action(client, action, a["name"], on)
        result = "ok" if action else "ok (no off action)"
    except Exception as e:
        err = safe_error(e, action)
        log.warning("automation %s: %s", a["name"], err)
        await pool.execute("UPDATE automation SET last_result = $2 WHERE id = $1", a["id"], err)
        return False
    await pool.execute("UPDATE automation SET active = $2, changed_at = $3, last_result = $4 WHERE id = $1",
                       a["id"], on, now, result)
    await pool.execute("INSERT INTO event (ts, kind, severity, message, data) VALUES ($1, 'automation', 'info', $2, "
                       "$3::jsonb)", now, f"{a['name']}: {'on' if on else 'off'}",
                       json.dumps({"automation_id": a["id"], "active": on}))
    return True


async def run_automations(pool, client, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    changed = 0
    for row in await pool.fetch("SELECT * FROM automation WHERE enabled ORDER BY id"):
        a = dict(row)
        try:
            want = await desired(pool, a, now)
        except Exception:
            log.exception("automation %s evaluation failed", a["name"])
            continue
        if want is None or want == a["active"]:
            continue
        if not want and a["changed_at"] and now - a["changed_at"] < timedelta(minutes=a["min_on_minutes"]):
            continue  # keep a started appliance running for its minimum time
        if await switch(pool, client, a, want, now):
            changed += 1
    return changed
