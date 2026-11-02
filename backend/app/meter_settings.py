"""Meter settings and API v2 pairing (F-SET-01). The collector reads the
`meter` setting at start and restarts itself within ~30 s when it changes."""
from __future__ import annotations

import json
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .auth import require_admin

router = APIRouter(prefix="/api/meter", tags=["meter"], dependencies=[Depends(require_admin)])
HOST = r"^[A-Za-z0-9.\-:\[\]]{1,253}$"
PAIR_NAME = "local/p1monitor"


class MeterSettings(BaseModel):
    host: str = Field(pattern=HOST)
    driver: Literal["v1", "v2"] = "v1"
    interval: float = Field(1.0, ge=0.5, le=60)


class PairRequest(BaseModel):
    host: str = Field(pattern=HOST)


async def _get(pool) -> dict:
    v = await pool.fetchval("SELECT value FROM setting WHERE key = 'meter'")
    return (json.loads(v) if isinstance(v, str) else v) or {}


async def _put(pool, value: dict) -> None:
    await pool.execute("INSERT INTO setting (key, value, updated_at) VALUES ('meter', $1::jsonb, now()) "
                       "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                       json.dumps(value))


def _public(s: dict) -> dict:
    out = {k: v for k, v in s.items() if k != "token"}
    out["has_token"] = bool(s.get("token"))
    return out


@router.get("/config", summary="Meter settings stored in the app (environment values apply when empty)")
async def get_config(request: Request):
    pool = request.app.state.pool
    active = await pool.fetchrow("SELECT meter_host AS host, driver FROM collector_status WHERE id = 1")
    return {**_public(await _get(pool)), "active": dict(active) if active else None}


@router.put("/config", summary="Change meter host, driver or poll interval; the collector restarts")
async def put_config(body: MeterSettings, request: Request):
    pool = request.app.state.pool
    s = await _get(pool)
    if body.driver == "v2" and not s.get("token"):
        raise HTTPException(409, "pair with the meter first (API v2 needs a token)")
    s.update(body.model_dump())
    await _put(pool, s)
    return _public(s)


async def _request_token(host: str) -> httpx.Response:
    # LAN device with a HomeWizard-issued certificate for a serial-based name.
    async with httpx.AsyncClient(verify=False, timeout=5) as client:  # noqa: S501
        return await client.post(f"https://{host}/api/user", json={"name": PAIR_NAME},
                                 headers={"X-Api-Version": "2"})


@router.post("/pair", summary="One pairing attempt with an API v2 meter; repeat while the button is pressed")
async def pair(body: PairRequest, request: Request):
    try:
        r = await _request_token(body.host)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"meter not reachable over HTTPS ({type(e).__name__}); "
                                 "API v2 needs firmware 6.00 or newer")
    if r.status_code == 403:
        return {"status": "press_button"}
    if r.status_code >= 400:
        raise HTTPException(502, f"meter answered HTTP {r.status_code}")
    token = r.json().get("token")
    if not token:
        raise HTTPException(502, "meter did not return a token")
    pool = request.app.state.pool
    s = await _get(pool)
    s.update({"host": body.host, "token": token})
    await _put(pool, s)
    return {"status": "paired", **_public(s)}
