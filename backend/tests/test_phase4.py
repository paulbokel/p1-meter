"""Phase 4: API tokens, Prometheus metrics, power quality, meter settings, MQTT payloads."""
import json
from datetime import datetime, timedelta, timezone

import asyncpg
import httpx
import pytest

from app import meter_settings
from app.main import app
from app.metrics import render
from app.mqtt_bridge import SENSORS, discovery, payload
from tests.test_api import DSN, login, run

UTC = timezone.utc


def test_mqtt_discovery_and_payload():
    msgs = dict(discovery("homeassistant", "p1monitor", "p1monitor"))
    cfg = msgs["homeassistant/sensor/p1monitor/import_t1_kwh/config"]
    assert cfg["state_class"] == "total_increasing" and cfg["device_class"] == "energy"
    assert cfg["state_topic"] == "p1monitor/state" and cfg["value_template"] == "{{ value_json.import_t1_kwh }}"
    assert "homeassistant/sensor/p1monitor/today_gas_m3/config" in msgs
    assert len(msgs) == len(SENSORS)                                  # unique topics
    assert len({c["unique_id"] for c in msgs.values()}) == len(SENSORS)
    p = payload({"ts": datetime(2026, 1, 1, tzinfo=UTC), "power_w": 5.0, "gas_m3": None})
    assert p == {"ts": "2026-01-01T00:00:00+00:00", "power_w": 5.0}


def test_metrics_render():
    now = datetime(2026, 1, 1, 0, 0, 10, tzinfo=UTC)
    text = render({"ts": now - timedelta(seconds=4), "power_w": -120.0, "power_l1_w": 10.0, "import_t1_kwh": 1.5,
                   "import_t2_kwh": 12345.678, "sag_l1_count": 3}, {"online": True, "consecutive_failures": 0}, 1000, now)
    assert "# TYPE p1_energy_import_kwh_total counter" in text
    assert 'p1_energy_import_kwh_total{tariff="2"} 12345.678' in text      # full precision
    assert "p1_power_watts -120.0" in text and "p1_last_reading_age_seconds 4.0" in text
    assert 'p1_phase_power_watts{phase="L2"}' not in text             # missing values are left out
    assert "p1_collector_online 1" in text


def test_api_tokens_are_read_only_and_revocable():
    async def body(c):
        await c.post("/api/auth/setup", json={"username": "paul", "password": "longenough"})
        r = await c.post("/api/tokens", json={"name": "grafana", "expires_days": 30})
        assert r.status_code == 201
        tok, tid = r.json()["token"], r.json()["id"]
        assert tok.startswith("p1_") and r.json()["prefix"] == tok[:10]
        listed = (await c.get("/api/tokens")).json()
        assert listed[0]["name"] == "grafana" and "token" not in listed[0] and "token_hash" not in listed[0]

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
            h = {"Authorization": f"Bearer {tok}"}
            assert (await api.get("/api/today", headers=h)).status_code == 200
            assert (await api.get("/metrics", headers=h)).status_code == 200
            me = (await api.get("/api/auth/me", headers=h)).json()
            assert me["role"] == "viewer"                               # never admin rights
            assert (await api.get("/api/tokens", headers=h)).status_code == 403
            assert (await api.patch("/api/auth/me", headers=h, json={"language": "en"})).status_code == 403
            assert (await api.get("/api/today", headers={"Authorization": "Bearer p1_wrong"})).status_code == 401
            assert (await api.get("/metrics")).status_code == 401
            con = await asyncpg.connect(DSN)
            assert await con.fetchval("SELECT last_used_at IS NOT NULL FROM api_token WHERE id = $1", tid)
            await con.close()
            assert (await c.delete(f"/api/tokens/{tid}")).status_code == 204
            assert (await api.get("/api/today", headers=h)).status_code == 401
        assert (await c.delete("/api/tokens/999")).status_code == 404
    run(body)


def test_quality_endpoints():
    async def body(c):
        await c.post("/api/auth/setup", json={"username": "paul", "password": "longenough"})
        con = await asyncpg.connect(DSN)
        t0 = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=30)
        rows = []
        for i in range(30 * 60):                                         # one reading a minute for 30 h
            t = t0 + timedelta(minutes=i)
            v = 230.0 + (10 if i == 100 else 0)
            rows.append((t, 1, v, v - 1, v + 1, 0 + (1 if i >= 600 else 0), 2 + (1 if i >= 1200 else 0)))
        await con.copy_records_to_table("reading_raw", records=rows, columns=[
            "ts", "epoch", "voltage_l1_v", "voltage_l2_v", "voltage_l3_v", "sag_l1_count", "power_fail_count"])
        await con.execute("INSERT INTO event (ts, kind, severity, message) VALUES (now(), 'voltage_sag', 'info', "
                          "'Voltage sag on L1'), (now(), 'alert', 'info', 'x')")
        await con.close()
        q = {"start": t0.isoformat(), "end": (t0 + timedelta(hours=30)).isoformat()}
        v = (await c.get("/api/quality/voltage", params=q)).json()
        assert v["resolution"] == "5m" and v["phases"] == [1, 2, 3]
        assert max(r["l1_max_v"] for r in v["rows"]) == 240.0
        q7 = {"start": (t0 - timedelta(days=5)).isoformat(), "end": (t0 + timedelta(hours=30)).isoformat()}
        h = (await c.get("/api/quality/voltage", params=q7)).json()
        assert h["resolution"] == "1h" and max(r["l1_max_v"] for r in h["rows"]) == 240.0
        d = (await c.get("/api/quality/daily", params=q7)).json()["rows"]
        assert sum(r["sags"] for r in d) == 1 and sum(r["power_fail"] for r in d) == 1
        ev = (await c.get("/api/quality/events")).json()
        assert [e["kind"] for e in ev] == ["voltage_sag"]
    run(body)


def test_meter_settings_and_pairing(monkeypatch):
    async def body(c):
        await c.post("/api/auth/setup", json={"username": "paul", "password": "longenough"})
        cfg = (await c.get("/api/meter/config")).json()
        assert cfg["has_token"] is False and "host" not in cfg
        assert (await c.put("/api/meter/config", json={"host": "10.0.0.2", "driver": "v2"})).status_code == 409
        assert (await c.put("/api/meter/config", json={"host": "x; rm -rf", "driver": "v1"})).status_code == 422
        r = await c.put("/api/meter/config", json={"host": "10.0.0.2", "driver": "v1", "interval": 2})
        assert {k: r.json()[k] for k in ("host", "driver", "interval", "has_token")} == \
            {"host": "10.0.0.2", "driver": "v1", "interval": 2.0, "has_token": False}

        presses = iter([403, 200])

        async def fake_request(host):
            assert host == "10.0.0.2"
            code = next(presses)
            return httpx.Response(code, json={"token": "ABC123"} if code == 200 else {"error": "x"})
        monkeypatch.setattr(meter_settings, "_request_token", fake_request)
        assert (await c.post("/api/meter/pair", json={"host": "10.0.0.2"})).json() == {"status": "press_button"}
        r = (await c.post("/api/meter/pair", json={"host": "10.0.0.2"})).json()
        assert r["status"] == "paired" and r["has_token"] and "token" not in r
        assert (await c.put("/api/meter/config", json={"host": "10.0.0.2", "driver": "v2"})).status_code == 200
        con = await asyncpg.connect(DSN)
        stored = json.loads(await con.fetchval("SELECT value FROM setting WHERE key = 'meter'"))
        await con.close()
        assert stored["token"] == "ABC123" and stored["driver"] == "v2"
    run(body)


def test_env_token_counts_when_collector_runs_v2():
    async def body(c):
        await c.post("/api/auth/setup", json={"username": "paul", "password": "longenough"})
        con = await asyncpg.connect(DSN)
        await con.execute("UPDATE collector_status SET driver = 'v2', meter_host = '10.0.0.3' WHERE id = 1")
        await con.close()
        cfg = (await c.get("/api/meter/config")).json()
        assert cfg["has_token"] and cfg["active"]["driver"] == "v2"
        r = await c.put("/api/meter/config", json={"host": "10.0.0.3", "driver": "v2", "interval": 2})
        assert r.status_code == 200
    run(body)
