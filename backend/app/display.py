"""Display endpoints for wall tablets and e-paper screens (F-LIVE-05).

    GET /api/display          compact JSON for ESPHome / custom displays
    GET /api/display.png      ready-to-show image (1-bit by default), e.g. 800x480

Both accept a read-only API token as a Bearer header or, for devices that can
only fetch a URL, as ?token=... (keep such URLs on the LAN).
"""
from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials

from . import costs
from .auth import COOKIE, User, _token_user, bearer, current_user
from .costs import ZONE

router = APIRouter(prefix="/api", tags=["display"])


async def display_user(request: Request, token: str | None = Query(None, description="API token (alternative to the header)"),
                       cred: HTTPAuthorizationCredentials | None = Depends(bearer)) -> User:
    if token and cred is None and not request.cookies.get(COOKIE):
        return await _token_user(request, token)
    return await current_user(request, cred)


async def summary(pool, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ZONE)
    day0 = datetime(local.year, local.month, local.day, tzinfo=ZONE)
    async with pool.acquire() as con:
        live = await con.fetchrow("SELECT ts, power_w, active_tariff FROM reading_raw ORDER BY ts DESC LIMIT 1")
        today = await con.fetchrow(
            "SELECT sum(import_t1_kwh + import_t2_kwh) AS import_kwh, sum(export_t1_kwh + export_t2_kwh) AS export_kwh, "
            "sum(gas_m3) AS gas_m3 FROM energy_15m WHERE bucket >= $1", day0)
        hour = await con.fetch("SELECT bucket, power_avg_w FROM reading_1m WHERE bucket >= $1 ORDER BY bucket",
                               now - timedelta(hours=1))
        price = await con.fetchrow("SELECT price FROM price WHERE kind = 'elec' AND ts <= $1 AND ts + duration > $1 "
                                   "ORDER BY ts DESC LIMIT 1", now)
    cost_rows = await costs.cost_rows(pool, day0, day0 + timedelta(days=1))
    contracts = await costs.load_contracts(pool)
    cost = costs.summarize(cost_rows, contracts)["total_eur"] if contracts.on(local.date()) else None
    age = (now - live["ts"]).total_seconds() if live else None
    r = lambda v, d=3: round(v, d) if v is not None else None  # noqa: E731
    return {
        "time": now.isoformat(),
        "power_w": r(live["power_w"], 0) if live else None,
        "tariff": live["active_tariff"] if live else None,
        "fresh": age is not None and age < 120,
        "today": {"import_kwh": r(today["import_kwh"]), "export_kwh": r(today["export_kwh"]),
                  "gas_m3": r(today["gas_m3"]), "cost_eur": r(cost, 2)},
        "price_spot_eur_kwh": r(price["price"], 4) if price else None,
        "last_hour_w": [r(h["power_avg_w"], 0) for h in hour],
    }


@router.get("/display", summary="Compact live summary for displays")
async def display_json(request: Request, _: User = Depends(display_user)):
    return await summary(request.app.state.pool)


def _fmt(v, d=1, unit=""):
    if v is None:
        return "-"
    s = f"{v:,.{d}f}".replace(",", " ").replace(".", ",")
    return f"{s}{unit}"


def render_png(s: dict, w: int, h: int, grey: bool) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(img)
    u = h / 480
    f = lambda px: ImageFont.load_default(size=max(10, int(px * u)))  # noqa: E731
    pad = int(24 * u)
    p = s["power_w"]
    label = "Teruglevering" if p is not None and p < 0 else "Afname"
    d.text((pad, pad), label, font=f(28), fill=0)
    d.text((pad, pad + 34 * u), _fmt(abs(p) if p is not None else None, 0, " W"), font=f(110), fill=0)
    t = datetime.fromisoformat(s["time"]).astimezone(ZONE).strftime("%H:%M")
    d.text((w - pad, pad), t if s["fresh"] else f"{t}  geen data", font=f(28), fill=0, anchor="ra")

    td = s["today"]
    # The built-in font has no € or ³, so plain ASCII units are used.
    cols = [("Afname", _fmt(td["import_kwh"], 1, " kWh")), ("Terug", _fmt(td["export_kwh"], 1, " kWh")),
            ("Gas", _fmt(td["gas_m3"], 2, " m3")), ("Kosten", "-" if td["cost_eur"] is None else "EUR " + _fmt(td["cost_eur"], 2))]
    y = int(196 * u)
    cw = (w - 2 * pad) / len(cols)
    for i, (k, v) in enumerate(cols):
        x = pad + i * cw
        d.text((x, y), f"{k} vandaag" if i == 0 else k, font=f(22), fill=96 if grey else 0)
        d.text((x, y + 28 * u), v, font=f(36), fill=0)

    # last hour, net power around a zero line
    vals = [v for v in s["last_hour_w"] if v is not None]
    top, bottom = int(330 * u), h - pad
    d.line((pad, top - 40 * u, w - pad, top - 40 * u), fill=0, width=max(1, int(u)))
    d.text((pad, top - 34 * u), "Afgelopen uur", font=f(18), fill=96 if grey else 0)
    if len(vals) > 1:
        lo, hi = min(min(vals), 0), max(max(vals), 0)
        span = (hi - lo) or 1
        ymap = lambda v: bottom - (v - lo) / span * (bottom - top)  # noqa: E731
        zero = ymap(0)
        d.line((pad, zero, w - pad, zero), fill=160 if grey else 0, width=1)
        step = (w - 2 * pad) / (len(vals) - 1)
        pts = [(pad + i * step, ymap(v)) for i, v in enumerate(vals)]
        d.line(pts, fill=0, width=max(2, int(3 * u)))
    out = img if grey else img.convert("1", dither=Image.Dither.NONE)
    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


@router.get("/display.png", summary="Display image for e-paper screens", response_class=Response)
async def display_png(request: Request, w: int = Query(800, ge=200, le=2000), h: int = Query(480, ge=120, le=2000),
                      grey: bool = False, _: User = Depends(display_user)):
    s = await summary(request.app.state.pool)
    return Response(render_png(s, w, h, grey), media_type="image/png", headers={"Cache-Control": "no-store"})
