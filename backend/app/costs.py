"""Cost engine (F-COST): contracts with validity periods, fixed / day-night /
dynamic prices, fixed charges, energy-tax rebate, netting and projection.

Energy always comes from counter deltas. Fixed and day/night contracts are
linear in energy per tariff, so they are priced per local day; days under a
dynamic contract are priced per 15 minutes with the day-ahead price.
"""
from __future__ import annotations

import calendar
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Europe/Amsterdam")


@dataclass
class Contract:
    id: int
    name: str
    kind: str
    valid_from: date
    valid_to: date | None
    elec_import_t1: float
    elec_import_t2: float
    elec_export_t1: float
    elec_export_t2: float
    netting: bool
    gas_price: float
    gas_dynamic: bool
    vat_pct: float
    fixed_monthly_elec: float
    fixed_monthly_gas: float
    energy_tax_rebate_year: float
    advance_monthly: float

    @classmethod
    def from_row(cls, r) -> "Contract":
        d = dict(r)
        return cls(**{k: (float(d[k]) if hasattr(d[k], "as_tuple") else d[k])
                      for k in cls.__dataclass_fields__})

    def covers(self, day: date) -> bool:
        return self.valid_from <= day and (self.valid_to is None or day < self.valid_to)

    def import_price(self, tariff: int, spot: float | None) -> float:
        surcharge = self.elec_import_t1 if (tariff == 1 or self.kind == "fixed") else self.elec_import_t2
        if self.kind == "dynamic":
            return (spot or 0.0) * (1 + self.vat_pct / 100) + surcharge
        return surcharge

    def export_price(self, tariff: int, spot: float | None) -> float:
        """Compensation per exported kWh as priced per bucket."""
        if self.netting:
            return self.import_price(tariff, spot)
        return self.feed_in_price(tariff, spot)

    def feed_in_price(self, tariff: int, spot: float | None) -> float:
        """Compensation for export above import (or always, without netting)."""
        fee = self.elec_export_t1 if (tariff == 1 or self.kind == "fixed") else self.elec_export_t2
        if self.kind == "dynamic":
            return (spot or 0.0) * (1 + self.vat_pct / 100) - fee
        return fee

    def gas(self, spot: float | None) -> float:
        if self.gas_dynamic:
            return (spot or 0.0) * (1 + self.vat_pct / 100) + self.gas_price
        return self.gas_price

    def fixed_per_day(self, day: date) -> float:
        days_in_month = calendar.monthrange(day.year, day.month)[1]
        days_in_year = 366 if calendar.isleap(day.year) else 365
        return ((self.fixed_monthly_elec + self.fixed_monthly_gas) / days_in_month
                - self.energy_tax_rebate_year / days_in_year)


class Contracts:
    def __init__(self, contracts: list[Contract]):
        self.items = sorted(contracts, key=lambda c: c.valid_from)

    def on(self, day: date) -> Contract | None:
        found = None
        for c in self.items:  # latest valid_from wins when periods overlap
            if c.covers(day):
                found = c
        return found


class Prices:
    """Step function of day-ahead prices: each price holds until the next one."""

    def __init__(self, rows: list[tuple[datetime, float, timedelta]]):
        rows = sorted(rows)
        self.ts = [r[0] for r in rows]
        self.rows = rows

    def at(self, t: datetime) -> float | None:
        i = bisect_right(self.ts, t) - 1
        if i < 0:
            return None
        start, price, dur = self.rows[i]
        return price if t < start + dur else None


def local_day(t: datetime) -> date:
    return t.astimezone(ZONE).date()


def price_bucket(row: dict, c: Contract | None, elec_spot: float | None, gas_spot: float | None) -> dict:
    """Variable cost for one bucket of energy (a 15 min row, or a whole day for non-dynamic)."""
    if c is None:
        return {"import_eur": None, "export_eur": None, "gas_eur": None, "missing_price": False}
    imp = exp = 0.0
    for tariff in (1, 2):
        imp += (row.get(f"import_t{tariff}_kwh") or 0) * c.import_price(tariff, elec_spot)
        exp += (row.get(f"export_t{tariff}_kwh") or 0) * c.export_price(tariff, elec_spot)
    gas = (row.get("gas_m3") or 0) * c.gas(gas_spot)
    missing = (c.kind == "dynamic" and elec_spot is None) or (c.gas_dynamic and gas_spot is None)
    return {"import_eur": imp, "export_eur": exp, "gas_eur": gas, "missing_price": missing}


def netting_correction(rows: list[dict], contracts: Contracts) -> float:
    """With netting, export is credited at the import price only up to the
    imported kWh; the excess earns the feed-in price. Applied over a period."""
    imp = sum((r.get("import_kwh") or 0) for r in rows if r.get("netting"))
    exp = sum((r.get("export_kwh") or 0) for r in rows if r.get("netting"))
    if exp <= imp or exp == 0:
        return 0.0
    credited = sum((r.get("export_eur") or 0) for r in rows if r.get("netting"))
    feed_in = sum((r.get("feed_in_eur") or 0) for r in rows if r.get("netting"))
    excess_share = (exp - imp) / exp
    # Replace the excess share of the at-import-price credit with feed-in.
    return excess_share * (credited - feed_in)


async def load_contracts(pool) -> Contracts:
    rows = await pool.fetch("SELECT * FROM tariff_contract ORDER BY valid_from")
    return Contracts([Contract.from_row(r) for r in rows])


async def load_prices(pool, kind: str, start: datetime, end: datetime) -> Prices:
    rows = await pool.fetch(
        "SELECT ts, price, duration FROM price WHERE kind = $1 AND ts < $3 AND ts + duration > $2 ORDER BY ts",
        kind, start, end)
    return Prices([(r["ts"], r["price"], r["duration"]) for r in rows])


async def cost_rows(pool, start: datetime, end: datetime) -> list[dict]:
    """Per local day: energy, variable costs, fixed costs. Dynamic days priced per 15 min."""
    contracts = await load_contracts(pool)
    days = await pool.fetch(
        "SELECT bucket AS t, import_t1_kwh, import_t2_kwh, export_t1_kwh, export_t2_kwh, gas_m3 "
        "FROM energy_1d WHERE bucket >= $1 AND bucket < $2 ORDER BY 1", start, end)
    need_spot = any(c.kind == "dynamic" or c.gas_dynamic for c in contracts.items)
    quarter: dict[date, list] = {}
    elec = gas = Prices([])
    if need_spot and days:
        elec = await load_prices(pool, "elec", start, end)
        gas = await load_prices(pool, "gas", start, end)
        dyn_days = {local_day(r["t"]) for r in days
                    if (c := contracts.on(local_day(r["t"]))) and (c.kind == "dynamic" or c.gas_dynamic)}
        if dyn_days:
            q = await pool.fetch(
                "SELECT bucket AS t, import_t1_kwh, import_t2_kwh, export_t1_kwh, export_t2_kwh, gas_m3 "
                "FROM energy_15m WHERE bucket >= $1 AND bucket < $2 ORDER BY 1", start, end)
            for r in q:
                d = local_day(r["t"])
                if d in dyn_days:
                    quarter.setdefault(d, []).append(dict(r))

    out = []
    for r in days:
        row = dict(r)
        day = local_day(row["t"])
        c = contracts.on(day)
        if day in quarter:
            parts = [price_bucket(q, c, elec.at(q["t"]), gas.at(q["t"])) for q in quarter[day]]
            var = {k: sum(p[k] for p in parts) for k in ("import_eur", "export_eur", "gas_eur")}
            var["missing_price"] = any(p["missing_price"] for p in parts)
            feed_in = sum(sum((q.get(f"export_t{t}_kwh") or 0) * c.feed_in_price(t, elec.at(q["t"]))
                              for t in (1, 2)) for q in quarter[day])
        else:
            var = price_bucket(row, c, None, None)
            feed_in = sum((row.get(f"export_t{t}_kwh") or 0) * c.feed_in_price(t, None) for t in (1, 2)) if c else 0
        fixed = c.fixed_per_day(day) if c else None
        imp_kwh = (row["import_t1_kwh"] or 0) + (row["import_t2_kwh"] or 0)
        exp_kwh = (row["export_t1_kwh"] or 0) + (row["export_t2_kwh"] or 0)
        total = None
        if c:
            total = var["import_eur"] - var["export_eur"] + var["gas_eur"] + fixed
        out.append({"t": row["t"], "day": day, "contract_id": c.id if c else None, "netting": bool(c and c.netting),
                    "import_kwh": imp_kwh, "export_kwh": exp_kwh, "gas_m3": row["gas_m3"] or 0,
                    "import_eur": var["import_eur"], "export_eur": var["export_eur"], "gas_eur": var["gas_eur"],
                    "feed_in_eur": feed_in, "fixed_eur": fixed, "total_eur": total,
                    "missing_price": var["missing_price"]})
    return out


def summarize(rows: list[dict], contracts: Contracts | None = None) -> dict:
    priced = [r for r in rows if r["total_eur"] is not None]
    s = {k: round(sum(r[k] for r in priced), 2) for k in ("import_eur", "export_eur", "gas_eur", "fixed_eur")}
    s["netting_correction_eur"] = round(netting_correction(priced, contracts), 2) if contracts else 0.0
    s["total_eur"] = round(sum(r["total_eur"] for r in priced) + s["netting_correction_eur"], 2)
    s["unpriced_days"] = len(rows) - len(priced)
    s["missing_prices"] = any(r["missing_price"] for r in priced)
    return s


def group(rows: list[dict], resolution: str) -> list[dict]:
    """Group daily cost rows into months or years (local calendar)."""
    if resolution == "1d":
        return rows
    key = (lambda d: date(d.year, d.month, 1)) if resolution == "1mo" else (lambda d: date(d.year, 1, 1))
    out: dict[date, dict] = {}
    for r in rows:
        k = key(r["day"])
        g = out.setdefault(k, {"t": datetime(k.year, k.month, 1, tzinfo=ZONE), "day": k, **{
            f: 0.0 for f in ("import_kwh", "export_kwh", "gas_m3", "import_eur", "export_eur", "gas_eur",
                             "feed_in_eur", "fixed_eur", "total_eur")}, "missing_price": False, "netting": r["netting"]})
        for f in ("import_kwh", "export_kwh", "gas_m3", "import_eur", "export_eur", "gas_eur", "feed_in_eur",
                  "fixed_eur", "total_eur"):
            if r[f] is not None:
                g[f] += r[f]
        g["missing_price"] |= r["missing_price"]
    return list(out.values())


def cheapest_block(prices: list[tuple[datetime, float]], hours: float, step: timedelta) -> dict | None:
    """Cheapest contiguous block of `hours` within the given consecutive price periods."""
    n = max(1, round(timedelta(hours=hours) / step))
    if len(prices) < n:
        return None
    best = None
    window = sum(p for _, p in prices[:n])
    for i in range(len(prices) - n + 1):
        if i:
            window += prices[i + n - 1][1] - prices[i - 1][1]
        contiguous = prices[i + n - 1][0] - prices[i][0] == step * (n - 1)
        if contiguous and (best is None or window < best[1]):
            best = (i, window)
    if best is None:
        return None
    i, total = best
    return {"start": prices[i][0], "end": prices[i + n - 1][0] + step, "avg_price": total / n}
