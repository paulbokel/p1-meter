"""Turn HomeWizard P1 API v1 JSON into a flat Reading."""
from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

AMS = ZoneInfo("Europe/Amsterdam")

COUNTER_FIELDS = ("import_t1_kwh", "import_t2_kwh", "export_t1_kwh", "export_t2_kwh", "gas_m3")


@dataclass
class Reading:
    ts: datetime
    epoch: int = 1
    active_tariff: int | None = None
    import_t1_kwh: float | None = None
    import_t2_kwh: float | None = None
    export_t1_kwh: float | None = None
    export_t2_kwh: float | None = None
    power_w: float | None = None
    power_l1_w: float | None = None
    power_l2_w: float | None = None
    power_l3_w: float | None = None
    voltage_l1_v: float | None = None
    voltage_l2_v: float | None = None
    voltage_l3_v: float | None = None
    current_l1_a: float | None = None
    current_l2_a: float | None = None
    current_l3_a: float | None = None
    sag_l1_count: int | None = None
    sag_l2_count: int | None = None
    sag_l3_count: int | None = None
    swell_l1_count: int | None = None
    swell_l2_count: int | None = None
    swell_l3_count: int | None = None
    power_fail_count: int | None = None
    long_power_fail_count: int | None = None
    gas_m3: float | None = None
    gas_ts: datetime | None = None

    @classmethod
    def columns(cls) -> list[str]:
        return [f.name for f in fields(cls)]

    def row(self) -> tuple:
        return tuple(getattr(self, c) for c in self.columns())


# v1 JSON key -> Reading attribute
V1_MAP = {
    "active_tariff": "active_tariff",
    "total_power_import_t1_kwh": "import_t1_kwh",
    "total_power_import_t2_kwh": "import_t2_kwh",
    "total_power_export_t1_kwh": "export_t1_kwh",
    "total_power_export_t2_kwh": "export_t2_kwh",
    "active_power_w": "power_w",
    "active_power_l1_w": "power_l1_w",
    "active_power_l2_w": "power_l2_w",
    "active_power_l3_w": "power_l3_w",
    "active_voltage_l1_v": "voltage_l1_v",
    "active_voltage_l2_v": "voltage_l2_v",
    "active_voltage_l3_v": "voltage_l3_v",
    "active_current_l1_a": "current_l1_a",
    "active_current_l2_a": "current_l2_a",
    "active_current_l3_a": "current_l3_a",
    "voltage_sag_l1_count": "sag_l1_count",
    "voltage_sag_l2_count": "sag_l2_count",
    "voltage_sag_l3_count": "sag_l3_count",
    "voltage_swell_l1_count": "swell_l1_count",
    "voltage_swell_l2_count": "swell_l2_count",
    "voltage_swell_l3_count": "swell_l3_count",
    "any_power_fail_count": "power_fail_count",
    "long_power_fail_count": "long_power_fail_count",
    "total_gas_m3": "gas_m3",
}

INT_ATTRS = {f.name for f in fields(Reading) if f.type in ("int | None",)}


def parse_gas_timestamp(value) -> datetime | None:
    """DSMR gas timestamp YYMMDDhhmmss in local time (int or str)."""
    if value in (None, "", 0):
        return None
    s = str(int(value)).zfill(12)
    try:
        local = datetime(2000 + int(s[0:2]), int(s[2:4]), int(s[4:6]),
                         int(s[6:8]), int(s[8:10]), int(s[10:12]), tzinfo=AMS)
    except ValueError:
        return None
    return local.astimezone(timezone.utc)


def parse_v1(data: dict, ts: datetime | None = None) -> Reading:
    r = Reading(ts=ts or datetime.now(timezone.utc))
    for key, attr in V1_MAP.items():
        v = data.get(key)
        if v is None:
            continue
        setattr(r, attr, int(v) if attr in INT_ATTRS else float(v))

    # Single-tariff meters only report totals.
    if r.import_t1_kwh is None and data.get("total_power_import_kwh") is not None:
        r.import_t1_kwh = float(data["total_power_import_kwh"])
        r.import_t2_kwh = 0.0
    if r.export_t1_kwh is None and data.get("total_power_export_kwh") is not None:
        r.export_t1_kwh = float(data["total_power_export_kwh"])
        r.export_t2_kwh = 0.0

    # Single-phase meters report a single current.
    if r.current_l1_a is None and data.get("active_current_a") is not None:
        r.current_l1_a = float(data["active_current_a"])
    if r.voltage_l1_v is None and data.get("active_voltage_v") is not None:
        r.voltage_l1_v = float(data["active_voltage_v"])

    # Gas: older firmware uses top-level fields, newer uses "external".
    gas_ts = data.get("gas_timestamp")
    for ext in data.get("external") or []:
        if ext.get("type") == "gas_meter" and ext.get("value") is not None:
            r.gas_m3 = float(ext["value"])
            gas_ts = ext.get("timestamp", gas_ts)
            break
    r.gas_ts = parse_gas_timestamp(gas_ts)
    return r


def meter_info_v1(data: dict, device: dict | None = None) -> dict:
    info = {k: data.get(k) for k in ("meter_model", "smr_version", "unique_id", "wifi_ssid", "wifi_strength")
            if data.get(k) is not None}
    if device:
        info.update({k: device.get(k) for k in ("product_name", "product_type", "serial",
                                                 "firmware_version", "api_version") if device.get(k) is not None})
    return info


class EpochTracker:
    """Starts a new epoch when any energy counter goes backwards (meter swap)."""

    TOLERANCE = 0.001  # kWh / m3; ignore float jitter

    def __init__(self, epoch: int = 1, last: dict | None = None):
        self.epoch = epoch
        self.last: dict[str, float] = dict(last or {})

    def check(self, r: Reading) -> bool:
        """Assign epoch to the reading. Returns True if a new epoch started."""
        decreased = False
        for c in COUNTER_FIELDS:
            v = getattr(r, c)
            prev = self.last.get(c)
            if v is not None and prev is not None and v < prev - self.TOLERANCE:
                decreased = True
        if decreased:
            self.epoch += 1
        for c in COUNTER_FIELDS:
            v = getattr(r, c)
            if v is not None:
                self.last[c] = v
        r.epoch = self.epoch
        return decreased
