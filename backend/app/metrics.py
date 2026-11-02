"""Prometheus metrics endpoint (F-EXP-04). Scrape with a read-only API token:

    authorization: { type: Bearer, credentials: p1_... }
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse

from .auth import current_user

router = APIRouter(tags=["metrics"], dependencies=[Depends(current_user)])

GAUGES = [
    ("p1_power_watts", "Net active power, negative is export", [("power_w", {})]),
    ("p1_phase_power_watts", "Active power per phase",
     [(f"power_l{i}_w", {"phase": f"L{i}"}) for i in (1, 2, 3)]),
    ("p1_phase_voltage_volts", "Voltage per phase", [(f"voltage_l{i}_v", {"phase": f"L{i}"}) for i in (1, 2, 3)]),
    ("p1_phase_current_amperes", "Current per phase", [(f"current_l{i}_a", {"phase": f"L{i}"}) for i in (1, 2, 3)]),
    ("p1_active_tariff", "Active tariff (1 = low/night, 2 = normal/day)", [("active_tariff", {})]),
]
COUNTERS = [
    ("p1_energy_import_kwh_total", "Imported electricity meter reading",
     [("import_t1_kwh", {"tariff": "1"}), ("import_t2_kwh", {"tariff": "2"})]),
    ("p1_energy_export_kwh_total", "Exported electricity meter reading",
     [("export_t1_kwh", {"tariff": "1"}), ("export_t2_kwh", {"tariff": "2"})]),
    ("p1_gas_m3_total", "Gas meter reading", [("gas_m3", {})]),
    ("p1_voltage_sags_total", "Voltage sags counted by the meter",
     [(f"sag_l{i}_count", {"phase": f"L{i}"}) for i in (1, 2, 3)]),
    ("p1_voltage_swells_total", "Voltage swells counted by the meter",
     [(f"swell_l{i}_count", {"phase": f"L{i}"}) for i in (1, 2, 3)]),
    ("p1_power_failures_total", "Power failures counted by the meter",
     [("power_fail_count", {"kind": "any"}), ("long_power_fail_count", {"kind": "long"})]),
]


def _labels(d: dict) -> str:
    return "{" + ",".join(f'{k}="{v}"' for k, v in d.items()) + "}" if d else ""


def render(reading: dict | None, status: dict | None, db_size: int | None, now: datetime) -> str:
    out: list[str] = []

    def block(name, help_, kind, samples):
        samples = [(lab, v) for lab, v in samples if v is not None]
        if not samples:
            return
        out.append(f"# HELP {name} {help_}")
        out.append(f"# TYPE {name} {kind}")
        out.extend(f"{name}{_labels(lab)} {float(v)!r}" for lab, v in samples)

    r = reading or {}
    for name, help_, cols in GAUGES:
        block(name, help_, "gauge", [(lab, r.get(c)) for c, lab in cols])
    # Meter readings are monotonic in normal operation; exposed as counters so
    # increase()/rate() work. A meter swap shows up as a counter reset.
    for name, help_, cols in COUNTERS:
        block(name, help_, "counter", [(lab, r.get(c)) for c, lab in cols])
    if r.get("ts"):
        block("p1_last_reading_age_seconds", "Seconds since the last stored reading", "gauge",
              [({}, (now - r["ts"]).total_seconds())])
    if status:
        block("p1_collector_online", "1 when the collector reaches the meter", "gauge",
              [({}, 1 if status.get("online") else 0)])
        block("p1_collector_consecutive_failures", "Consecutive failed meter reads", "gauge",
              [({}, status.get("consecutive_failures"))])
    block("p1_database_size_bytes", "Size of the database", "gauge", [({}, db_size)])
    return "\n".join(out) + "\n"


@router.get("/metrics", response_class=PlainTextResponse, summary="Prometheus metrics")
async def metrics(request: Request):
    pool = request.app.state.pool
    async with pool.acquire() as con:
        reading = await con.fetchrow("SELECT * FROM reading_raw ORDER BY ts DESC LIMIT 1")
        status = await con.fetchrow("SELECT online, consecutive_failures FROM collector_status WHERE id = 1")
        db_size = await con.fetchval("SELECT pg_database_size(current_database())")
    body = render(dict(reading) if reading else None, dict(status) if status else None, db_size,
                  datetime.now(timezone.utc))
    return PlainTextResponse(body, media_type="text/plain; version=0.0.4")
