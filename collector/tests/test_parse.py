from datetime import datetime, timezone

from p1collector.parse import EpochTracker, Reading, parse_gas_timestamp, parse_v1

SAMPLE_V1 = {
    "wifi_ssid": "home", "wifi_strength": 100, "smr_version": 50, "meter_model": "ISKRA 2M550T-1012",
    "unique_id": "00112233445566778899AABBCCDDEEFF", "active_tariff": 2,
    "total_power_import_kwh": 13779.338, "total_power_import_t1_kwh": 10830.511,
    "total_power_import_t2_kwh": 2948.827, "total_power_export_kwh": 1234.567,
    "total_power_export_t1_kwh": 234.567, "total_power_export_t2_kwh": 1000.0,
    "active_power_w": -543, "active_power_l1_w": -676, "active_power_l2_w": 133, "active_power_l3_w": 0,
    "active_current_a": 6, "active_current_l1_a": -4, "active_current_l2_a": 2, "active_current_l3_a": 0,
    "active_voltage_l1_v": 230.1, "active_voltage_l2_v": 231.0, "active_voltage_l3_v": 229.8,
    "voltage_sag_l1_count": 1, "voltage_sag_l2_count": 1, "voltage_sag_l3_count": 0,
    "voltage_swell_l1_count": 0, "voltage_swell_l2_count": 0, "voltage_swell_l3_count": 0,
    "any_power_fail_count": 4, "long_power_fail_count": 5,
    "total_gas_m3": 2569.646, "gas_timestamp": 210606140010,
    "external": [{"unique_id": "x", "type": "gas_meter", "timestamp": 230125220957, "value": 2570.123,
                  "unit": "m3"}],
}


def test_parse_v1_full():
    r = parse_v1(SAMPLE_V1, ts=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert r.import_t1_kwh == 10830.511 and r.import_t2_kwh == 2948.827
    assert r.export_t1_kwh == 234.567 and r.export_t2_kwh == 1000.0
    assert r.power_w == -543 and r.power_l1_w == -676
    assert r.voltage_l2_v == 231.0 and r.current_l1_a == -4
    assert r.sag_l1_count == 1 and isinstance(r.sag_l1_count, int)
    assert r.active_tariff == 2 and isinstance(r.active_tariff, int)
    assert r.power_fail_count == 4 and r.long_power_fail_count == 5
    # "external" gas meter wins over legacy top-level field
    assert r.gas_m3 == 2570.123
    assert r.gas_ts == datetime(2023, 1, 25, 21, 9, 57, tzinfo=timezone.utc)  # 22:09:57 CET
    assert len(r.row()) == len(Reading.columns())


def test_parse_v1_single_tariff_single_phase():
    r = parse_v1({"total_power_import_kwh": 100.0, "total_power_export_kwh": 5.0,
                  "active_power_w": 300, "active_current_a": 1.3, "active_voltage_v": 232.0})
    assert r.import_t1_kwh == 100.0 and r.import_t2_kwh == 0.0
    assert r.export_t1_kwh == 5.0 and r.export_t2_kwh == 0.0
    assert r.current_l1_a == 1.3 and r.voltage_l1_v == 232.0
    assert r.gas_m3 is None and r.gas_ts is None


def test_gas_timestamp_summer_time():
    assert parse_gas_timestamp(260701120000) == datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
    assert parse_gas_timestamp(None) is None
    assert parse_gas_timestamp(999999999999) is None


def test_epoch_tracker_starts_new_epoch_on_decrease():
    t = EpochTracker()
    a = parse_v1({"total_power_import_t1_kwh": 100.0, "total_power_import_t2_kwh": 50.0})
    b = parse_v1({"total_power_import_t1_kwh": 100.5, "total_power_import_t2_kwh": 50.0})
    c = parse_v1({"total_power_import_t1_kwh": 0.2, "total_power_import_t2_kwh": 0.1})  # meter swap
    d = parse_v1({"total_power_import_t1_kwh": 0.3, "total_power_import_t2_kwh": 0.1})
    assert [t.check(x) for x in (a, b, c, d)] == [False, False, True, False]
    assert [x.epoch for x in (a, b, c, d)] == [1, 1, 2, 2]


def test_epoch_tracker_ignores_jitter_and_missing():
    t = EpochTracker(3, {"import_t1_kwh": 10.0})
    r = parse_v1({"total_power_import_t1_kwh": 9.9995})
    assert t.check(r) is False and r.epoch == 3
    assert t.check(parse_v1({})) is False
