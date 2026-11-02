"""Fill a database with synthetic history for UI development and demos.

    DATABASE_URL=postgresql://p1:pw@localhost:5432/p1demo python devtools/seed_history.py --days 400

Writes one reading per minute (so the 15 min / daily aggregates have data)
into reading_raw and refreshes the continuous aggregates. Do not run this
against your real database.
"""
import argparse
import asyncio
import math
import os
import random
from datetime import datetime, timedelta, timezone

import asyncpg


def profile(t: datetime):
    local_h = (t.hour + 1) % 24 + t.minute / 60
    doy = t.timetuple().tm_yday
    winter = 0.5 + 0.5 * math.cos((doy - 15) / 365 * 2 * math.pi)  # 1 in January, 0 in July
    base = 160 + 40 * winter
    load = base + 500 * max(0, math.sin((local_h - 6) / 17 * math.pi)) * (0.7 + 0.6 * random.random())
    if 17 <= local_h < 19.5:
        load += 900 * random.random()
    day_len = 8 + 8 * (1 - winter)
    sunrise = 13 - day_len / 2
    solar = 0
    if sunrise < local_h < sunrise + day_len:
        solar = (3200 - 2400 * winter) * math.sin((local_h - sunrise) / day_len * math.pi) * random.uniform(0.5, 1)
    gas_m3_per_min = (0.0012 + 0.006 * winter) * (1.5 if 6 <= local_h < 22 else 0.5)
    return load - solar, gas_m3_per_min


async def main(days: int):
    con = await asyncpg.connect(os.environ["DATABASE_URL"])
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=5)
    t = end - timedelta(days=days)
    imp = [5000.0, 4000.0]
    exp = [300.0, 900.0]
    gas = 3000.0
    batch = []
    cols = ["ts", "epoch", "active_tariff", "import_t1_kwh", "import_t2_kwh", "export_t1_kwh", "export_t2_kwh",
            "power_w", "power_l1_w", "power_l2_w", "power_l3_w", "voltage_l1_v", "voltage_l2_v", "voltage_l3_v",
            "gas_m3"]
    while t < end:
        p, g = profile(t)
        local = t + timedelta(hours=1)
        tariff = 1 if local.hour < 7 or local.hour >= 23 or local.weekday() >= 5 else 2
        kwh = abs(p) / 60000
        (imp if p >= 0 else exp)[tariff - 1] += kwh
        gas += g
        v = [230 + random.uniform(-3, 3) for _ in range(3)]
        batch.append((t, 1, tariff, imp[0], imp[1], exp[0], exp[1], p, p * .5, p * .3, p * .2, *v, gas))
        if len(batch) >= 20000:
            await con.copy_records_to_table("reading_raw", records=batch, columns=cols)
            batch = []
        t += timedelta(minutes=1)
    if batch:
        await con.copy_records_to_table("reading_raw", records=batch, columns=cols)
    for view in ("reading_1m", "counter_15m"):
        await con.execute(f"CALL refresh_continuous_aggregate('{view}', NULL, NULL)")
    await con.close()
    print(f"seeded {days} days")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=400)
    asyncio.run(main(ap.parse_args().days))
