"""Alerts, costs and base load against a real TimescaleDB (TEST_DATABASE_URL)."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from app import alerts, notify
from tests.test_api import DSN, run

pytestmark = pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")
UTC = timezone.utc


async def seed(con, end: datetime, days: int, spike_hour: datetime | None = None):
    """Readings every 15 min: 0.5 kWh/h import, 3 kWh during spike_hour."""
    t = end - timedelta(days=days)
    imp, rows = 1000.0, []
    while t < end:
        rows.append((t, 1, imp, 0.0, 0.0, 0.0, 0.0, 500.0))
        step = 0.75 if spike_hour and spike_hour <= t < spike_hour + timedelta(hours=1) else 0.125
        imp += step
        t += timedelta(minutes=15)
    await con.copy_records_to_table(
        "reading_raw", records=rows,
        columns=["ts", "epoch", "import_t1_kwh", "import_t2_kwh", "export_t1_kwh", "export_t2_kwh", "gas_m3",
                 "power_w"])


def test_hourly_anomaly_fires_once_and_notifies(monkeypatch):
    sent = []

    async def fake_send(pool, client, channel, title, body, data=None):
        sent.append((channel["name"], title, body))

    monkeypatch.setattr(notify, "send", fake_send)

    async def body(c):
        await c.post("/api/auth/setup", json={"username": "paul", "password": "longenough"})
        now = datetime(2026, 10, 14, 19, 5, tzinfo=UTC)
        spike = datetime(2026, 10, 14, 18, 0, tzinfo=UTC)
        con = await asyncpg.connect(DSN)
        # the reading at spike+1h closes the spike hour's counter
        await seed(con, now.replace(minute=0), 60, spike)
        await con.execute("UPDATE alert_rule SET enabled = (kind = 'anomaly_hour')")
        await con.close()
        r = await c.post("/api/channels", json={"kind": "ntfy", "name": "phone",
                                                "config": {"topic": "p1-test", "token": "s3cret"}})
        assert r.json()["config"]["token"] == "••••"

        pool = c._transport.app.state.pool
        assert await alerts.run_rules(pool, None, now) == 1
        assert await alerts.run_rules(pool, None, now) == 0              # same hour not re-evaluated
        assert len(sent) == 1 and "Unusual consumption" in sent[0][2]
        ev = (await c.get("/api/events", params={"kind": "alert"})).json()
        assert len(ev) == 1 and ev[0]["notify_result"] == "phone: ok" and ev[0]["data"]["value"] == pytest.approx(2.375)  # 3 of 4 spiked quarters land in 18:xx

        # snoozed: the alert is recorded but not sent
        rule_id = ev[0]["rule_id"]
        await c.post(f"/api/alerts/rules/{rule_id}/snooze", json={"minutes": 600})
        await pool.execute("UPDATE alert_rule SET last_fired_at = NULL, state = '{}' WHERE id = $1", rule_id)
        assert await alerts.run_rules(pool, None, now) == 1
        assert len(sent) == 1
        ev = (await c.get("/api/events", params={"kind": "alert"})).json()
        assert ev[0]["notify_result"] == "snoozed"
    run(body)


def test_costs_api_with_contract_change():
    async def body(c):
        await c.post("/api/auth/setup", json={"username": "paul", "password": "longenough"})
        con = await asyncpg.connect(DSN)
        await seed(con, datetime(2026, 3, 3, 0, tzinfo=UTC) - timedelta(hours=1), 4)   # 27 Feb - 2 Mar local
        await con.close()
        base = {"kind": "fixed", "elec_import_t2": 0, "netting": True, "gas_price": 0, "fixed_monthly_elec": 0}
        await c.post("/api/contracts", json={**base, "name": "old", "valid_from": "2026-01-01",
                                              "valid_to": "2026-03-01", "elec_import_t1": 0.20})
        await c.post("/api/contracts", json={**base, "name": "new", "valid_from": "2026-03-01",
                                              "elec_import_t1": 0.40, "advance_monthly": 100})
        r = (await c.get("/api/costs", params={"start": "2026-02-28T00:00:00+01:00",
                                               "end": "2026-03-02T00:00:00+01:00"})).json()
        feb28, mar1 = r["rows"]
        assert feb28["t"] == "2026-02-27T23:00:00+00:00"                  # local midnight
        assert feb28["import_eur"] == pytest.approx(12 * 0.20, rel=1e-6)
        assert mar1["import_eur"] == pytest.approx(12 * 0.40, rel=1e-6)
        assert r["summary"]["total_eur"] == pytest.approx(12 * 0.6, abs=0.01)
        bad = await c.post("/api/contracts", json={**base, "name": "x", "valid_from": "2026-03-01",
                                                   "valid_to": "2026-02-01", "elec_import_t1": 0.4})
        assert bad.status_code == 422
    run(body)


def test_baseload_backfill_and_api():
    async def body(c):
        await c.post("/api/auth/setup", json={"username": "paul", "password": "longenough"})
        con = await asyncpg.connect(DSN)
        end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        await seed(con, end, 10)
        await con.execute("CALL refresh_continuous_aggregate('reading_1m', NULL, NULL)")
        await con.close()
        from app import baseload
        pool = c._transport.app.state.pool
        assert await baseload.backfill(pool) >= 8
        b = (await c.get("/api/baseload", params={"days": 30})).json()
        assert b["current_w"] == pytest.approx(500, abs=1)
        assert b["kwh_year"] == pytest.approx(500 * 8.766, rel=1e-3)
    run(body)




def test_cheapest_skips_started_period_and_dynamic_projection_needs_spot():
    async def body(c):
        await c.post("/api/auth/setup", json={"username": "paul", "password": "longenough"})
        now = datetime.now(UTC)
        hour = now.replace(minute=0, second=0, microsecond=0)
        con = await asyncpg.connect(DSN)
        # the running hour is the cheapest, but it has already started
        await con.executemany("INSERT INTO price (kind, source, ts, price, duration) VALUES ('elec', 'test', $1, $2, interval '1 hour')",
                              [(hour + timedelta(hours=i), 0.01 if i == 0 else 0.10 + i / 100) for i in range(6)])
        await con.close()
        r = (await c.get("/api/prices/cheapest", params={"hours": 2})).json()
        assert datetime.fromisoformat(r["block"]["start"]) >= now

        await c.post("/api/contracts", json={"name": "dyn", "kind": "dynamic", "valid_from": "2020-01-01",
                                              "elec_import_t1": 0.15, "elec_import_t2": 0.15, "gas_price": 0})
        p = (await c.get("/api/costs/projection")).json()
        assert p["method"] == "average_so_far"   # no year of last-year data: falls back, never prices at spot 0
    run(body)
