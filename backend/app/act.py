"""Phase 5 API: automations (F-AUTO), the cheapest-hours planner (F-COST-05)
and the display endpoints for kiosks and e-paper screens (F-LIVE-05)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from . import automations, costs
from .auth import current_user, require_admin
from .insights import _row

router = APIRouter(prefix="/api", dependencies=[Depends(current_user)])


class AutomationIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    enabled: bool = True
    trigger_kind: Literal["surplus", "price_below", "cheapest_hours"]
    trigger_params: dict = {}
    action: dict
    off_action: dict | None = None
    min_on_minutes: int = Field(default=0, ge=0, le=1440)

    @field_validator("action", "off_action")
    @classmethod
    def valid_action(cls, v):
        if v is None:
            return v
        kind = v.get("kind")
        if kind not in automations.ACTION_FIELDS:
            raise ValueError(f"action kind must be one of {', '.join(automations.ACTION_FIELDS)}")
        required = {"mqtt": ["topic"], "homeassistant": ["url", "token", "service"], "webhook": ["url"]}[kind]
        missing = [f for f in required if not v.get(f)]
        if missing:
            raise ValueError(f"{kind} action needs {', '.join(missing)}")
        if kind in ("homeassistant", "webhook") and not str(v["url"]).startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        if kind == "homeassistant" and "." not in v["service"]:
            raise ValueError("service must look like domain.service, e.g. switch.turn_on")
        return {k: val for k, val in v.items() if k == "kind" or k in automations.ACTION_FIELDS[kind]}

    @model_validator(mode="after")
    def valid_params(self):
        p = {**automations.TRIGGERS[self.trigger_kind], **self.trigger_params}
        try:
            if self.trigger_kind == "surplus":
                assert float(p["watts"]) > 0 and 1 <= int(p["minutes"]) <= 120
            elif self.trigger_kind == "price_below":
                float(p["eur_kwh"])
                assert p["basis"] in ("spot", "all_in")
            else:
                assert 0 < float(p["hours"]) <= 24
                automations.window_for(datetime.now(timezone.utc), p["from"], p["to"])
        except (AssertionError, ValueError, TypeError, KeyError):
            raise ValueError(f"invalid trigger parameters for {self.trigger_kind}")
        self.trigger_params = p
        return self


def _out(r) -> dict:
    d = _row(r)
    for k in ("trigger_params", "action", "off_action"):
        if isinstance(d.get(k), str):
            d[k] = json.loads(d[k])
    d["action"] = automations.redact_action(d["action"])
    d["off_action"] = automations.redact_action(d["off_action"])
    return d


@router.get("/automations", tags=["automations"])
async def list_automations(request: Request):
    return [_out(r) for r in await request.app.state.pool.fetch("SELECT * FROM automation ORDER BY id")]


@router.get("/automations/kinds", tags=["automations"])
async def automation_kinds():
    return {"triggers": automations.TRIGGERS, "actions": automations.ACTION_FIELDS}


@router.post("/automations", tags=["automations"], status_code=201, dependencies=[Depends(require_admin)])
async def create_automation(body: AutomationIn, request: Request):
    r = await request.app.state.pool.fetchrow(
        "INSERT INTO automation (name, enabled, trigger_kind, trigger_params, action, off_action, min_on_minutes) "
        "VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6::jsonb, $7) RETURNING *",
        body.name, body.enabled, body.trigger_kind, json.dumps(body.trigger_params), json.dumps(body.action),
        json.dumps(body.off_action) if body.off_action else None, body.min_on_minutes)
    return _out(r)


@router.put("/automations/{aid}", tags=["automations"], dependencies=[Depends(require_admin)])
async def update_automation(aid: int, body: AutomationIn, request: Request):
    pool = request.app.state.pool
    old = await pool.fetchrow("SELECT * FROM automation WHERE id = $1", aid)
    if not old:
        raise HTTPException(404, "automation not found")
    action = automations.keep_secrets(body.action, automations._j(old["action"]))
    off = automations.keep_secrets(body.off_action, automations._j(old["off_action"]) if old["off_action"] else None)
    # A changed trigger invalidates a stored plan.
    reset_state = body.trigger_kind != old["trigger_kind"] or body.trigger_params != automations._j(old["trigger_params"])
    r = await pool.fetchrow(
        "UPDATE automation SET name = $2, enabled = $3, trigger_kind = $4, trigger_params = $5::jsonb, "
        "action = $6::jsonb, off_action = $7::jsonb, min_on_minutes = $8, "
        "state = CASE WHEN $9 THEN '{}'::jsonb ELSE state END WHERE id = $1 RETURNING *",
        aid, body.name, body.enabled, body.trigger_kind, json.dumps(body.trigger_params), json.dumps(action),
        json.dumps(off) if off else None, body.min_on_minutes, reset_state)
    if old["active"] and not body.enabled:
        # Disabling an automation that switched something on switches it off.
        async with httpx.AsyncClient() as client:
            await automations.switch(pool, client, dict(r), False, datetime.now(timezone.utc))
        r = await pool.fetchrow("SELECT * FROM automation WHERE id = $1", aid)
    return _out(r)


@router.delete("/automations/{aid}", tags=["automations"], status_code=204, dependencies=[Depends(require_admin)])
async def delete_automation(aid: int, request: Request):
    await request.app.state.pool.execute("DELETE FROM automation WHERE id = $1", aid)


@router.post("/automations/{aid}/test", tags=["automations"], dependencies=[Depends(require_admin)],
             summary="Run the on or off action now, without changing the automation's state")
async def test_automation(aid: int, request: Request, state: Literal["on", "off"] = "on"):
    a = await request.app.state.pool.fetchrow("SELECT * FROM automation WHERE id = $1", aid)
    if not a:
        raise HTTPException(404, "automation not found")
    action = automations._j(a["action"] if state == "on" else a["off_action"])
    if not action:
        raise HTTPException(409, "this automation has no off action")
    async with httpx.AsyncClient() as client:
        try:
            await automations.run_action(client, action, a["name"], state == "on")
        except Exception as e:
            raise HTTPException(502, automations.safe_error(e, action))
    return {"ok": True}


@router.get("/automations/{aid}/plan", tags=["automations"], summary="The current cheapest-hours plan")
async def automation_plan(aid: int, request: Request):
    a = await request.app.state.pool.fetchrow("SELECT * FROM automation WHERE id = $1", aid)
    if not a:
        raise HTTPException(404, "automation not found")
    if a["trigger_kind"] != "cheapest_hours":
        raise HTTPException(409, "not a cheapest-hours automation")
    p = {**automations.TRIGGERS["cheapest_hours"], **automations._j(a["trigger_params"])}
    start, end = automations.window_for(datetime.now(timezone.utc), p["from"], p["to"])
    plan = await automations.make_plan(request.app.state.pool, p, start, end)
    return plan or {"window": [start.isoformat(), end.isoformat()], "slots": [], "pending": True}


# --- cheapest-hours planner ------------------------------------------------------

@router.get("/prices/plan", tags=["costs"], summary="Cheapest hours before a deadline (one block or separate periods)")
async def price_plan(request: Request, hours: float = Query(2, gt=0, le=24), before: datetime | None = None,
                     contiguous: bool = True):
    now = datetime.now(timezone.utc)
    if before is not None and before.tzinfo is None:
        raise HTTPException(422, "before needs a timezone offset")
    end = before or now + timedelta(days=2)
    rows = await request.app.state.pool.fetch(
        "SELECT ts, price, duration FROM price WHERE kind = 'elec' AND ts >= $1 AND ts + duration <= $2 ORDER BY ts",
        now, end)
    plan = costs.plan_cheapest([(r["ts"], r["price"], r["duration"]) for r in rows], hours, contiguous)
    if not plan:
        return {"available": False, "known_until": rows[-1]["ts"].isoformat() if rows else None}
    contracts = await costs.load_contracts(request.app.state.pool)
    c = contracts.on(plan["slots"][0][0].astimezone(costs.ZONE).date())
    all_in = c.import_price(1, plan["avg_price"]) if c and c.kind == "dynamic" else None
    avg_all = sum(r["price"] for r in rows) / len(rows)
    return {"available": True, "slots": [[s.isoformat(), e.isoformat()] for s, e in plan["slots"]],
            "avg_spot": round(plan["avg_price"], 5), "avg_all_in": round(all_in, 5) if all_in else None,
            "avg_spot_window": round(avg_all, 5)}
