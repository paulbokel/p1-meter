"""Phase 5: planner, automations (surplus / price / cheapest hours), display endpoints."""
import json
from datetime import datetime, timedelta, timezone

import asyncpg
import httpx
import pytest

from app import automations
from app.costs import ZONE, plan_cheapest
from app.display import render_png
from app.main import app
from tests.test_api import DSN, run

UTC = timezone.utc
H = timedelta(hours=1)


def test_plan_cheapest_block_and_separate_hours():
    t0 = datetime(2026, 11, 2, tzinfo=UTC)
    prices = [(t0 + i * H, p, H) for i, p in enumerate([0.30, 0.10, 0.25, 0.05, 0.06, 0.40])]
    sep = plan_cheapest(prices, 2, contiguous=False)
    assert sep["slots"] == [(t0 + 3 * H, t0 + 5 * H)] and sep["avg_price"] == pytest.approx(0.055)
    three = plan_cheapest(prices, 3, contiguous=False)
    assert three["slots"] == [(t0 + H, t0 + 2 * H), (t0 + 3 * H, t0 + 5 * H)]
    block = plan_cheapest(prices, 3, contiguous=True)
    assert block["slots"] == [(t0 + 2 * H, t0 + 5 * H)]
    assert plan_cheapest(prices[:1], 2) is None


def test_window_for_crosses_midnight_and_dst():
    def local(*a):
        return datetime(*a, tzinfo=ZONE).astimezone(UTC)
    # 23:30 → inside 22:00–07:00 that started today
    s, e = automations.window_for(local(2026, 11, 2, 23, 30), "22:00", "07:00")
    assert (s, e) == (local(2026, 11, 2, 22), local(2026, 11, 3, 7))
    # 03:00 → inside the window that started yesterday
    s, e = automations.window_for(local(2026, 11, 3, 3), "22:00", "07:00")
    assert s == local(2026, 11, 2, 22)
    # 12:00 → the next window
    s, e = automations.window_for(local(2026, 11, 3, 12), "22:00", "07:00")
    assert s == local(2026, 11, 3, 22)
    # whole day; DST change day lasts 25 h
    s, e = automations.window_for(local(2026, 10, 25, 12), "00:00", "00:00")
    assert (e - s) == timedelta(hours=25)


def test_secrets_are_redacted_and_kept():
    a = {"kind": "homeassistant", "url": "http://ha:8123", "token": "abc", "service": "switch.turn_on"}
    r = automations.redact_action(a)
    assert r["token"] == "••••" and automations.keep_secrets(r, a)["token"] == "abc"


def test_render_png():
    s = {"time": "2026-11-02T12:00:00+00:00", "power_w": -850, "tariff": 2, "fresh": True,
         "today": {"import_kwh": 4.2, "export_kwh": 6.1, "gas_m3": 1.23, "cost_eur": 0.85},
         "price_spot_eur_kwh": None, "last_hour_w": [200, -300, -800, -850]}
    png = render_png(s, 800, 480, grey=False)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 1000


async def seed_minutes(con, end: datetime, watts: list[float]):
    t = end - timedelta(minutes=len(watts))
    rows = []
    for w in watts:
        for s in range(0, 60, 10):
            rows.append((t + timedelta(seconds=s), 1, w))
        t += timedelta(minutes=1)
    await con.copy_records_to_table("reading_raw", records=rows, columns=["ts", "epoch", "power_w"])


def test_surplus_automation_switches_with_hysteresis(monkeypatch):
    calls = []

    async def fake_action(client, action, name, on, env=None):
        calls.append((action["topic"], on))
    monkeypatch.setattr(automations, "run_action", fake_action)

    async def body(c):
        await c.post("/api/auth/setup", json={"username": "paul", "password": "longenough"})
        r = await c.post("/api/automations", json={
            "name": "Boiler on surplus", "trigger_kind": "surplus", "trigger_params": {"watts": 1000, "minutes": 5},
            "action": {"kind": "mqtt", "topic": "boiler/set", "payload": "ON"},
            "off_action": {"kind": "mqtt", "topic": "boiler/set", "payload": "OFF"}, "min_on_minutes": 20})
        assert r.status_code == 201, r.text
        aid = r.json()["id"]
        bad = await c.post("/api/automations", json={"name": "x", "trigger_kind": "surplus",
                                                     "trigger_params": {"minutes": 0}, "action": {"kind": "mqtt"}})
        assert bad.status_code == 422

        now = datetime.now(UTC).replace(second=30, microsecond=0)
        minute = now.replace(second=0)
        con = await asyncpg.connect(DSN)
        pool = app.state.pool
        await seed_minutes(con, minute - timedelta(minutes=30), [-1500] * 30)   # 30 min of strong export
        client = httpx.AsyncClient()
        # window is the 5 minutes before `at`; mixed window keeps the state
        t1 = minute - timedelta(minutes=30) + timedelta(seconds=30)
        assert await automations.run_automations(pool, client, t1) == 1 and calls == [("boiler/set", True)]
        await seed_minutes(con, minute, [200] * 30)                              # then importing
        t2 = minute - timedelta(minutes=28)                                       # mixed: keep
        assert await automations.run_automations(pool, client, t2) == 0
        t3 = minute - timedelta(minutes=20)                                       # all import, but min-on 20 min
        assert await automations.run_automations(pool, client, t3) == 0
        t4 = minute - timedelta(minutes=5)
        assert await automations.run_automations(pool, client, t4) == 1 and calls[-1] == ("boiler/set", False)
        ev = await con.fetch("SELECT message FROM event WHERE kind = 'automation' ORDER BY ts")
        assert [e["message"] for e in ev] == ["Boiler on surplus: on", "Boiler on surplus: off"]
        await con.close()
        await client.aclose()
        listed = (await c.get("/api/automations")).json()
        assert listed[0]["active"] is False and listed[0]["last_result"] == "ok"
        assert (await c.delete(f"/api/automations/{aid}")).status_code == 204
    run(body)


def test_cheapest_hours_automation_and_planner(monkeypatch):
    calls = []

    async def fake_action(client, action, name, on, env=None):
        calls.append(on)
    monkeypatch.setattr(automations, "run_action", fake_action)

    async def body(c):
        await c.post("/api/auth/setup", json={"username": "paul", "password": "longenough"})
        now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        con = await asyncpg.connect(DSN)
        prices = [0.30] * 48
        prices[5], prices[6], prices[20] = 0.01, 0.02, 0.03
        await con.executemany("INSERT INTO price (kind, source, ts, price, duration) VALUES ('elec', 'test', $1, $2, "
                              "interval '1 hour')", [(now - 24 * H + i * H, p) for i, p in enumerate(prices)])
        await con.close()
        local_from = (now - 24 * H).astimezone(ZONE).strftime("%H:%M")
        r = await c.post("/api/automations", json={
            "name": "Dishwasher", "trigger_kind": "cheapest_hours",
            "trigger_params": {"hours": 2, "from": local_from, "to": local_from, "contiguous": True},
            "action": {"kind": "webhook", "url": "http://plug.local/on", "secret": "s3"}})
        assert r.status_code == 201, r.text
        assert r.json()["action"]["secret"] == "••••"
        aid = r.json()["id"]
        pool = app.state.pool
        client = httpx.AsyncClient()
        # inside the cheapest block (periods 5 and 6 of the window)
        assert await automations.run_automations(pool, client, now - 24 * H + 5 * H + timedelta(minutes=10)) == 1
        assert calls == [True]
        assert await automations.run_automations(pool, client, now - 24 * H + 8 * H) == 1 and calls == [True]   # off, no off action
        await client.aclose()
        # PUT with the redacted secret keeps the stored one
        a = (await c.get("/api/automations")).json()[0]
        assert (await c.put(f"/api/automations/{aid}", json={**{k: a[k] for k in (
            "name", "enabled", "trigger_kind", "trigger_params", "action", "off_action", "min_on_minutes")}})).status_code == 200
        con = await asyncpg.connect(DSN)
        stored = json.loads(await con.fetchval("SELECT action FROM automation WHERE id = $1", aid))
        await con.close()
        assert stored["secret"] == "s3"

        plan = (await c.get("/api/prices/plan", params={"hours": 1, "contiguous": "false"})).json()
        assert plan["available"] and len(plan["slots"]) == 1   # only future periods count
    run(body)


def test_display_endpoints_accept_query_token():
    async def body(c):
        await c.post("/api/auth/setup", json={"username": "paul", "password": "longenough"})
        tok = (await c.post("/api/tokens", json={"name": "epaper"})).json()["token"]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as dev:
            assert (await dev.get("/api/display")).status_code == 401
            j = await dev.get("/api/display", params={"token": tok})
            assert j.status_code == 200 and set(j.json()["today"]) == {"import_kwh", "export_kwh", "gas_m3", "cost_eur"}
            png = await dev.get("/api/display.png", params={"token": tok, "w": 400, "h": 300})
            assert png.status_code == 200 and png.headers["content-type"] == "image/png"
            assert (await dev.get("/api/today", params={"token": tok})).status_code == 401   # only display endpoints
    run(body)


def test_failed_off_on_disable_is_retried_by_worker(monkeypatch):
    fail = {"on": True}
    calls = []

    async def flaky(client, action, name, on, env=None):
        calls.append(on)
        if not on and fail["on"]:
            raise RuntimeError("broker down")
    monkeypatch.setattr(automations, "run_action", flaky)

    async def body(c):
        await c.post("/api/auth/setup", json={"username": "paul", "password": "longenough"})
        a = (await c.post("/api/automations", json={
            "name": "Boiler", "trigger_kind": "price_below", "trigger_params": {"eur_kwh": 1},
            "action": {"kind": "mqtt", "topic": "b/set"}, "off_action": {"kind": "mqtt", "topic": "b/set",
                                                                         "payload": "OFF"}})).json()
        con = await asyncpg.connect(DSN)
        await con.execute("UPDATE automation SET active = true WHERE id = $1", a["id"])
        body_ = {k: a[k] for k in ("name", "trigger_kind", "trigger_params", "action", "off_action", "min_on_minutes")}
        r = (await c.put(f"/api/automations/{a['id']}", json={**body_, "enabled": False})).json()
        assert r["active"] is True and "broker down" in r["last_result"]       # off failed, still on
        fail["on"] = False
        client = httpx.AsyncClient()
        assert await automations.run_automations(app.state.pool, client) == 1  # worker retries the off action
        await client.aclose()
        assert await con.fetchval("SELECT active FROM automation WHERE id = $1", a["id"]) is False
        await con.close()
        assert calls == [False, False]
    run(body)
