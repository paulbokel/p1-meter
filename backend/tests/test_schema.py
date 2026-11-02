"""Schema tests against a real TimescaleDB. Set TEST_DATABASE_URL to run them;
the database is wiped and migrated from scratch."""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import pytest

from app.migrate import migrate
from tests.dbreset import reset_schema

DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")
MIGRATIONS = Path(__file__).resolve().parents[2] / "db" / "migrations"


async def _fresh():
    await reset_schema(DSN)
    assert await migrate(DSN, MIGRATIONS) == sorted(p.name for p in MIGRATIONS.glob("*.sql"))
    assert await migrate(DSN, MIGRATIONS) == []  # idempotent
    return await asyncpg.connect(DSN)


async def _insert(con, ts, epoch, imp1, imp2=0.0, exp1=0.0, gas=0.0):
    await con.execute(
        "INSERT INTO reading_raw (ts, epoch, import_t1_kwh, import_t2_kwh, export_t1_kwh, export_t2_kwh, gas_m3, power_w)"
        " VALUES ($1, $2, $3, $4, $5, 0, $6, 100)", ts, epoch, imp1, imp2, exp1, gas)


def test_energy_from_counter_deltas_and_epochs():
    async def run():
        con = await _fresh()
        t0 = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=3)
        # bucket 0: counters end at 100.0; bucket 1: +0.5; gap (bucket 2 missing); bucket 3: +0.25
        await _insert(con, t0 + timedelta(minutes=1), 1, 99.9, gas=10.0)
        await _insert(con, t0 + timedelta(minutes=14), 1, 100.0, gas=10.0)
        await _insert(con, t0 + timedelta(minutes=29), 1, 100.5, exp1=0.1, gas=10.2)
        await _insert(con, t0 + timedelta(minutes=50), 1, 100.75, exp1=0.1, gas=10.2)
        # meter swap: counters restart at ~0 in epoch 2
        await _insert(con, t0 + timedelta(minutes=61), 2, 0.1)
        await _insert(con, t0 + timedelta(minutes=76), 2, 0.4)

        rows = await con.fetch("SELECT * FROM energy_15m ORDER BY bucket")
        imp = [r["import_t1_kwh"] for r in rows]
        assert imp[0] == 0                                 # no previous snapshot
        assert imp[1] == pytest.approx(0.5)
        assert imp[2] == pytest.approx(0.25)               # gap energy lands after the gap
        assert imp[3] == 0                                 # new epoch: no cross-epoch delta
        assert imp[4] == pytest.approx(0.3)
        assert all(v is None or v >= 0 for v in imp)
        assert rows[1]["export_t1_kwh"] == pytest.approx(0.1)
        assert rows[1]["gas_m3"] == pytest.approx(0.2)

        total = await con.fetchval("SELECT sum(import_t1_kwh) FROM energy_1h")
        assert total == pytest.approx(1.05)
        day = await con.fetchval("SELECT sum(import_t1_kwh) FROM energy_1d")
        assert day == pytest.approx(1.05)
        await con.close()

    asyncio.run(run())


def test_duplicate_timestamps_rejected():
    async def run():
        con = await _fresh()
        ts = datetime.now(timezone.utc)
        await _insert(con, ts, 1, 1.0)
        with pytest.raises(asyncpg.UniqueViolationError):
            await _insert(con, ts, 1, 1.0)
        await con.close()

    asyncio.run(run())
