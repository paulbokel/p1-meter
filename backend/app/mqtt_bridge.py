"""MQTT publication of live values with Home Assistant discovery (F-EXP-03).

Run as `python -m app.mqtt_bridge` (compose service `mqtt`, profile `mqtt`).
Publishes the latest reading every MQTT_INTERVAL seconds as one JSON message
on <topic>/state and today's totals on <topic>/today. Home Assistant picks the
sensors up automatically through MQTT discovery; the energy counters can be
used directly in the HA Energy dashboard.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime

import aiomqtt
import asyncpg

log = logging.getLogger("mqtt")

LIVE_COLS = ("ts, power_w, power_l1_w, power_l2_w, power_l3_w, voltage_l1_v, voltage_l2_v, voltage_l3_v, "
             "current_l1_a, current_l2_a, current_l3_a, active_tariff, import_t1_kwh, import_t2_kwh, "
             "export_t1_kwh, export_t2_kwh, gas_m3")


@dataclass
class Sensor:
    key: str
    name: str
    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = None
    topic: str = "state"
    icon: str | None = None
    precision: int | None = None


SENSORS = [
    Sensor("power_w", "Power", "W", "power", "measurement", precision=0),
    *[Sensor(f"power_l{i}_w", f"Power L{i}", "W", "power", "measurement", precision=0) for i in (1, 2, 3)],
    *[Sensor(f"voltage_l{i}_v", f"Voltage L{i}", "V", "voltage", "measurement", precision=1) for i in (1, 2, 3)],
    *[Sensor(f"current_l{i}_a", f"Current L{i}", "A", "current", "measurement", precision=2) for i in (1, 2, 3)],
    Sensor("active_tariff", "Active tariff", icon="mdi:clock-outline"),
    Sensor("import_t1_kwh", "Energy import T1", "kWh", "energy", "total_increasing", precision=3),
    Sensor("import_t2_kwh", "Energy import T2", "kWh", "energy", "total_increasing", precision=3),
    Sensor("export_t1_kwh", "Energy export T1", "kWh", "energy", "total_increasing", precision=3),
    Sensor("export_t2_kwh", "Energy export T2", "kWh", "energy", "total_increasing", precision=3),
    Sensor("gas_m3", "Gas", "m³", "gas", "total_increasing", precision=3),
    Sensor("import_kwh", "Import today", "kWh", "energy", "total", "today", precision=2),
    Sensor("export_kwh", "Export today", "kWh", "energy", "total", "today", precision=2),
    Sensor("gas_m3", "Gas today", "m³", "gas", "total", "today", precision=2),
]


def discovery(prefix: str, topic: str, node: str, sw_version: str = "0.4.0") -> list[tuple[str, dict]]:
    """(topic, payload) pairs for HA MQTT discovery, published retained."""
    device = {"identifiers": [node], "name": "P1 Energy Monitor", "manufacturer": "self-hosted",
              "model": "HomeWizard P1 (local)", "sw_version": sw_version}
    out = []
    for s in SENSORS:
        uid = f"{node}_{s.key}" if s.topic == "state" else f"{node}_{s.topic}_{s.key}"
        cfg = {
            "name": s.name, "unique_id": uid, "object_id": uid,
            "state_topic": f"{topic}/{s.topic}",
            "value_template": f"{{{{ value_json.{s.key} }}}}",
            "availability_topic": f"{topic}/status",
            "device": device,
        }
        for k in ("unit", "device_class", "state_class", "icon"):
            v = getattr(s, k)
            if v is not None:
                cfg["unit_of_measurement" if k == "unit" else k] = v
        if s.precision is not None:
            cfg["suggested_display_precision"] = s.precision
        out.append((f"{prefix}/sensor/{node}/{uid.removeprefix(node + '_')}/config", cfg))
    return out


def payload(row: dict | None) -> dict:
    if not row:
        return {}
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items() if v is not None}


class Bridge:
    def __init__(self, pool, client, topic: str, interval: float):
        self.pool, self.client, self.topic, self.interval = pool, client, topic, interval
        self.last_ts = None

    async def tick(self, with_today: bool) -> None:
        row = await self.pool.fetchrow(f"SELECT {LIVE_COLS} FROM reading_raw ORDER BY ts DESC LIMIT 1")
        if row and row["ts"] != self.last_ts:
            self.last_ts = row["ts"]
            await self.client.publish(f"{self.topic}/state", json.dumps(payload(dict(row))))
        if with_today:
            today = await self.pool.fetchrow(
                "SELECT round(sum(import_t1_kwh + import_t2_kwh)::numeric, 3)::float8 AS import_kwh, "
                "round(sum(export_t1_kwh + export_t2_kwh)::numeric, 3)::float8 AS export_kwh, "
                "round(sum(gas_m3)::numeric, 3)::float8 AS gas_m3 FROM energy_15m "
                "WHERE bucket >= time_bucket(interval '1 day', now(), 'Europe/Amsterdam')")
            await self.client.publish(f"{self.topic}/today", json.dumps(payload(dict(today))), retain=True)


async def run(env=os.environ) -> None:
    host = env.get("MQTT_HOST")
    if not host:
        raise SystemExit("MQTT_HOST is not set")
    topic = env.get("MQTT_TOPIC", "p1monitor").rstrip("/")
    prefix = env.get("MQTT_DISCOVERY_PREFIX", "homeassistant").rstrip("/")
    node = env.get("MQTT_NODE_ID", "p1monitor")
    interval = max(1.0, float(env.get("MQTT_INTERVAL", "5")))
    discovery_on = env.get("MQTT_DISCOVERY", "true").lower() in ("1", "true", "yes")
    pool = await asyncpg.create_pool(env["DATABASE_URL"], min_size=1, max_size=2)
    will = aiomqtt.Will(f"{topic}/status", "offline", retain=True)
    delay = 1
    while True:
        try:
            async with aiomqtt.Client(host, int(env.get("MQTT_PORT", "1883")), identifier=f"{node}-bridge",
                                      username=env.get("MQTT_USER") or None,
                                      password=env.get("MQTT_PASSWORD") or None, will=will) as client:
                log.info("connected to %s, publishing on %s/#", host, topic)
                delay = 1
                if discovery_on:
                    for t, cfg in discovery(prefix, topic, node):
                        await client.publish(t, json.dumps(cfg), retain=True)
                await client.publish(f"{topic}/status", "online", retain=True)
                bridge = Bridge(pool, client, topic, interval)
                n = 0
                while True:
                    await bridge.tick(with_today=n % max(1, int(60 / interval)) == 0)
                    n += 1
                    await asyncio.sleep(interval)
        except aiomqtt.MqttError as e:
            log.warning("MQTT connection lost (%s); retrying in %ss", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(run())
