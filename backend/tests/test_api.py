"""API tests (auth, users, history, export) against a real TimescaleDB.
Set TEST_DATABASE_URL to run them; the database is wiped."""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import httpx
import pytest

from app.main import app
from app.migrate import migrate

DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")
MIGRATIONS = Path(__file__).resolve().parents[2] / "db" / "migrations"


def run(coro_fn):
    async def wrapper():
        con = await asyncpg.connect(DSN)
        await con.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await con.close()
        await migrate(DSN, MIGRATIONS)
        app.state.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                await coro_fn(c)
        finally:
            await app.state.pool.close()
    asyncio.run(wrapper())


def login(c, username, password):
    return c.post("/api/auth/login", json={"username": username, "password": password})


def test_setup_login_roles_and_users():
    async def body(c):
        assert (await c.get("/api/status")).status_code == 401
        assert (await c.get("/api/auth/state")).json() == {"setup_required": True}
        r = await c.post("/api/auth/setup", json={"username": "paul", "password": "short"})
        assert r.status_code == 422
        r = await c.post("/api/auth/setup", json={"username": "paul", "password": "longenough"})
        assert r.status_code == 200 and r.json()["role"] == "admin"
        assert (await c.post("/api/auth/setup", json={"username": "x", "password": "longenough"})).status_code == 409
        assert (await c.get("/api/status")).status_code == 200

        r = await c.post("/api/users", json={"username": "anna", "password": "viewerpass", "role": "viewer"})
        assert r.status_code == 201
        anna = r.json()["id"]
        assert (await c.post("/api/users", json={"username": "anna", "password": "viewerpass"})).status_code == 409
        # last admin cannot be demoted
        assert (await c.patch("/api/users/1", json={"role": "viewer"})).status_code == 409

        await c.post("/api/auth/logout")
        assert (await c.get("/api/auth/me")).status_code == 401
        assert (await login(c, "anna", "wrong-password")).status_code == 401
        assert (await login(c, "nobody", "whatever1")).status_code == 401
        assert (await login(c, "anna", "viewerpass")).status_code == 200
        assert (await c.get("/api/today")).status_code == 200
        assert (await c.get("/api/users")).status_code == 403          # viewer
        assert (await c.patch("/api/auth/me", json={"language": "en"})).json()["language"] == "en"
        assert (await c.patch("/api/auth/me", json={"role": "admin"})).status_code == 403

        await c.post("/api/auth/logout")
        await login(c, "paul", "longenough")
        assert (await c.delete("/api/users/1")).status_code == 409      # self
        assert (await c.delete(f"/api/users/{anna}")).status_code == 204
    run(body)


def test_history_and_export():
    async def body(c):
        await c.post("/api/auth/setup", json={"username": "paul", "password": "longenough"})
        con = await asyncpg.connect(DSN)
        t0 = datetime(2026, 3, 29, 0, 0, tzinfo=timezone.utc) - timedelta(hours=1)  # local midnight, DST day
        rows = [(t0 + timedelta(minutes=15 * i + 5), 1, 100.0 + i * 0.1, 50.0, 1.0, 0.0, 10.0 + i * 0.01, 400.0)
                for i in range(4 * 30)]
        await con.copy_records_to_table(
            "reading_raw", records=rows,
            columns=["ts", "epoch", "import_t1_kwh", "import_t2_kwh", "export_t1_kwh", "export_t2_kwh", "gas_m3",
                     "power_w"])
        await con.close()

        q = {"start": "2026-03-29T00:00:00+01:00", "end": "2026-03-30T00:00:00+02:00"}
        r = (await c.get("/api/history", params={"resolution": "1h", **q})).json()
        assert len(r["rows"]) == 23                                    # 23-hour DST day
        assert r["totals"]["import_kwh"] == pytest.approx(0.1 * (4 * 23 - 1), abs=1e-6)

        d = (await c.get("/api/history", params={"resolution": "1d", **q})).json()
        assert len(d["rows"]) == 1 and d["rows"][0]["t"].startswith("2026-03-28T23:00")

        tot = (await c.get("/api/history/totals", params=q)).json()
        assert tot["import_kwh"] == pytest.approx(r["totals"]["import_kwh"])

        assert (await c.get("/api/history", params={"resolution": "15m", "start": "2026-01-01T00:00:00+01:00",
                                                    "end": "2026-12-01T00:00:00+01:00"})).status_code == 422
        assert (await c.get("/api/history", params={"resolution": "1h", "start": "2026-03-29T00:00:00",
                                                    "end": "2026-03-30T00:00:00"})).status_code == 422

        csv = await c.get("/api/export.csv", params={"resolution": "1h", **q})
        assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
        lines = csv.text.strip().splitlines()
        assert lines[0].startswith("time_local,t,import_t1_kwh") and len(lines) == 24
        assert lines[1].startswith("2026-03-29 00:00:00")

        p = (await c.get("/api/power", params=q)).json()
        assert p["resolution"] in ("1m", "15m")
    run(body)
