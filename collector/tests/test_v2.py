import asyncio
import json

import pytest
import websockets

from p1collector.config import MeterConfig
from p1collector.meter import MeterError, V2WsDriver, make_driver
from p1collector.parse import meter_info_v2, parse_v2

MEASUREMENT = {
    "protocol_version": 50, "meter_model": "ISKRA 2M550T-101", "unique_id": "00112233",
    "timestamp": "2026-11-02T14:00:00", "tariff": 2,
    "energy_import_kwh": 13779.338, "energy_import_t1_kwh": 10830.511, "energy_import_t2_kwh": 2948.827,
    "energy_export_kwh": 1234.5, "energy_export_t1_kwh": 234.5, "energy_export_t2_kwh": 1000.0,
    "power_w": -543, "power_l1_w": -676, "power_l2_w": 133, "power_l3_w": 0,
    "voltage_l1_v": 230.1, "voltage_l2_v": 229.8, "voltage_l3_v": 231.0,
    "current_a": 3.6, "current_l1_a": 2.9, "current_l2_a": 0.6, "current_l3_a": 0.1,
    "voltage_sag_l1_count": 1, "voltage_swell_l1_count": 0, "any_power_fail_count": 4, "long_power_fail_count": 5,
    "external": [{"unique_id": "4730", "type": "gas_meter", "timestamp": "2026-11-02T13:55:00",
                  "value": 2569.646, "unit": "m3"}],
}


def test_parse_v2():
    r = parse_v2(MEASUREMENT)
    assert (r.import_t1_kwh, r.import_t2_kwh, r.export_t2_kwh) == (10830.511, 2948.827, 1000.0)
    assert r.power_w == -543 and r.active_tariff == 2 and r.current_l1_a == 2.9
    assert r.sag_l1_count == 1 and isinstance(r.sag_l1_count, int)
    assert r.gas_m3 == 2569.646 and r.gas_ts.isoformat() == "2026-11-02T12:55:00+00:00"   # CET -> UTC
    single = parse_v2({"energy_import_kwh": 5.0, "energy_export_kwh": 1.0, "current_a": 2.0, "voltage_v": 231})
    assert (single.import_t1_kwh, single.import_t2_kwh, single.current_l1_a) == (5.0, 0.0, 2.0)
    assert meter_info_v2(MEASUREMENT, {"firmware_version": "6.02"})["smr_version"] == 50


def test_config_merge():
    env = MeterConfig.from_env({"P1_HOST": "10.0.0.5", "P1_POLL_INTERVAL": "2"})
    assert env.host == "10.0.0.5" and env.interval == 2
    m = env.merged({"host": "192.168.1.9", "driver": "v2", "interval": 0.1, "token": "abc"})
    assert (m.host, m.driver, m.interval, m.token) == ("192.168.1.9", "v2", 0.5, "abc")
    assert env.merged({"host": "bad host; rm", "driver": "v9"}) == env
    with pytest.raises(ValueError):
        make_driver(MeterConfig(driver="v2"))


async def fake_meter(ws, token="secret", sent=None):
    await ws.send(json.dumps({"type": "authorization_requested", "data": {"api_version": "2.0.0"}}))
    auth = json.loads(await ws.recv())
    if auth != {"type": "authorization", "data": token}:
        await ws.send(json.dumps({"type": "error", "data": {"message": "user:unauthorized"}}))
        return
    await ws.send(json.dumps({"type": "authorized"}))
    subs = [json.loads(await ws.recv())["data"] for _ in range(2)]
    if sent is not None:
        sent.extend(subs)
    await ws.send(json.dumps({"type": "device", "data": {"firmware_version": "6.02", "api_version": "2.0.0"}}))
    while True:
        await ws.send(json.dumps({"type": "measurement", "data": MEASUREMENT}))
        await asyncio.sleep(0.05)


def test_v2_websocket_driver_reads_and_reconnects():
    async def run():
        subs = []
        server = await websockets.serve(lambda ws: fake_meter(ws, sent=subs), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        d = V2WsDriver(f"ws://127.0.0.1:{port}/api/ws", "secret")
        with pytest.raises(MeterError):
            await d.read()                       # nothing received yet
        for _ in range(50):
            await asyncio.sleep(0.05)
            try:
                r = await d.read()
                break
            except MeterError:
                pass
        assert r.power_w == -543 and set(subs) == {"measurement", "device"}
        assert (await d.info())["firmware_version"] == "6.02"
        server.close()
        await server.wait_closed()
        await d.close()
    asyncio.run(run())


def test_v2_wrong_token_reports_error():
    async def run():
        server = await websockets.serve(lambda ws: fake_meter(ws), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        d = V2WsDriver(f"ws://127.0.0.1:{port}/api/ws", "wrong")
        with pytest.raises(MeterError):
            await d.read()
        await asyncio.sleep(0.3)
        with pytest.raises(MeterError, match="unauthorized"):
            await d.read()
        server.close()
        await d.close()
    asyncio.run(run())
