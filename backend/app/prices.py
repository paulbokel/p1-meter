"""Day-ahead price fetching (F-COST-03). Source: EnergyZero public API
(no key needed); prices are stored excl. VAT in EUR per kWh / m3."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger(__name__)

ENERGYZERO_URL = "https://api.energyzero.nl/v1/energyprices"
USAGE = {"elec": 1, "gas": 3}


def parse_energyzero(data: dict) -> list[tuple[datetime, float, timedelta]]:
    """Return (start, price, duration). Duration is derived from the spacing,
    so both hourly and quarter-hourly series work."""
    pts = []
    for p in data.get("Prices") or []:
        if p.get("price") is None or not p.get("readingDate"):
            continue
        ts = datetime.fromisoformat(p["readingDate"].replace("Z", "+00:00")).astimezone(timezone.utc)
        pts.append((ts, float(p["price"])))
    pts.sort()
    if not pts:
        return []
    gaps = [b[0] - a[0] for a, b in zip(pts, pts[1:]) if b[0] > a[0]]
    step = min(gaps) if gaps else timedelta(hours=1)
    return [(ts, price, step) for ts, price in pts]


async def fetch_energyzero(client: httpx.AsyncClient, kind: str, start: datetime, end: datetime):
    params = {
        "fromDate": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "tillDate": (end - timedelta(milliseconds=1)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.999Z"),
        "interval": 4,
        "usageType": USAGE[kind],
        "inclBtw": "false",
    }
    r = await client.get(ENERGYZERO_URL, params=params, timeout=20)
    r.raise_for_status()
    return parse_energyzero(r.json())


async def store_prices(pool, kind: str, source: str, rows) -> int:
    if not rows:
        return 0
    await pool.executemany(
        "INSERT INTO price (ts, kind, source, price, duration) VALUES ($1, $2, $3, $4, $5) "
        "ON CONFLICT (kind, ts) DO UPDATE SET price = EXCLUDED.price, source = EXCLUDED.source, "
        "duration = EXCLUDED.duration, fetched_at = now()",
        [(ts, kind, source, price, dur) for ts, price, dur in rows])
    return len(rows)


async def update_prices(pool, client: httpx.AsyncClient, days_back: int = 2) -> dict:
    """Fetch yesterday..tomorrow (tomorrow is published around 13:00 CET)."""
    from .costs import ZONE
    today = datetime.now(ZONE).replace(hour=0, minute=0, second=0, microsecond=0)
    start, end = today - timedelta(days=days_back), today + timedelta(days=2)
    result = {}
    for kind in ("elec", "gas"):
        rows = await fetch_energyzero(client, kind, start, end)
        result[kind] = await store_prices(pool, kind, "energyzero", rows)
    return result
