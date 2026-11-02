"""Pure unit tests for the cost engine, prices, base load and alert helpers."""
from datetime import date, datetime, time, timedelta, timezone

import pytest

from app.alerts import anomaly, in_quiet_hours
from app.baseload import baseload_for, step_changes, weighted_percentile
from app.costs import Contract, Contracts, Prices, cheapest_block, netting_correction, price_bucket
from app.prices import parse_energyzero

UTC = timezone.utc


def contract(**kw):
    base = dict(id=1, name="c", kind="fixed", valid_from=date(2026, 1, 1), valid_to=None, elec_import_t1=0.30,
                elec_import_t2=0.30, elec_export_t1=0.05, elec_export_t2=0.05, netting=False, gas_price=1.40,
                gas_dynamic=False, vat_pct=21, fixed_monthly_elec=31, fixed_monthly_gas=0,
                energy_tax_rebate_year=365, advance_monthly=100)
    base.update(kw)
    return Contract(**base)


ROW = {"import_t1_kwh": 2.0, "import_t2_kwh": 1.0, "export_t1_kwh": 0.5, "export_t2_kwh": 0.5, "gas_m3": 1.0}


def test_fixed_contract_without_netting():
    p = price_bucket(ROW, contract(), None, None)
    assert p["import_eur"] == pytest.approx(0.90)
    assert p["export_eur"] == pytest.approx(0.05)     # feed-in only
    assert p["gas_eur"] == pytest.approx(1.40)


def test_daynight_with_netting_credits_at_import_price():
    c = contract(kind="daynight", elec_import_t1=0.25, elec_import_t2=0.32, netting=True)
    p = price_bucket(ROW, c, None, None)
    assert p["import_eur"] == pytest.approx(2 * 0.25 + 0.32)
    assert p["export_eur"] == pytest.approx(0.5 * 0.25 + 0.5 * 0.32)


def test_dynamic_price_includes_vat_and_surcharge():
    c = contract(kind="dynamic", elec_import_t1=0.15, elec_export_t1=0.02)
    assert c.import_price(1, 0.10) == pytest.approx(0.10 * 1.21 + 0.15)
    assert c.feed_in_price(1, 0.10) == pytest.approx(0.10 * 1.21 - 0.02)
    p = price_bucket(ROW, c, None, None)
    assert p["missing_price"] is True


def test_fixed_charges_and_rebate_per_day():
    c = contract()
    assert c.fixed_per_day(date(2026, 1, 15)) == pytest.approx(31 / 31 - 365 / 365)
    assert c.fixed_per_day(date(2026, 2, 15)) == pytest.approx(31 / 28 - 1)


def test_contract_validity_latest_wins():
    a = contract(id=1, valid_from=date(2025, 1, 1), valid_to=date(2026, 1, 1))
    b = contract(id=2, valid_from=date(2026, 1, 1))
    cs = Contracts([b, a])
    assert cs.on(date(2025, 12, 31)).id == 1
    assert cs.on(date(2026, 1, 1)).id == 2
    assert cs.on(date(2024, 6, 1)) is None


def test_netting_caps_credit_at_imported_kwh():
    rows = [{"netting": True, "import_kwh": 100, "export_kwh": 150, "export_eur": 150 * 0.30,
             "feed_in_eur": 150 * 0.05}]
    # 50 of 150 kWh exceed import: their credit drops from 0.30 to 0.05
    assert netting_correction(rows, None) == pytest.approx(50 * (0.30 - 0.05))
    rows[0]["export_kwh"] = 80
    assert netting_correction(rows, None) == 0


def test_prices_step_function_and_cheapest_block():
    t0 = datetime(2026, 11, 2, 0, tzinfo=UTC)
    h = timedelta(hours=1)
    pr = Prices([(t0 + h * i, p, h) for i, p in enumerate([0.3, 0.1, 0.05, 0.2, 0.4])])
    assert pr.at(t0 + timedelta(minutes=90)) == 0.1
    assert pr.at(t0 + 5 * h) is None
    best = cheapest_block([(t0 + h * i, p) for i, p in enumerate([0.3, 0.1, 0.05, 0.2, 0.4])], 2, h)
    assert best["start"] == t0 + h and best["avg_price"] == pytest.approx(0.075)


def test_parse_energyzero_quarter_hours():
    data = {"Prices": [{"price": 0.1, "readingDate": "2026-11-01T23:00:00Z"},
                       {"price": 0.2, "readingDate": "2026-11-01T23:15:00Z"},
                       {"price": 0.3, "readingDate": "2026-11-01T23:30:00Z"}]}
    rows = parse_energyzero(data)
    assert [r[1] for r in rows] == [0.1, 0.2, 0.3]
    assert rows[0][2] == timedelta(minutes=15) and rows[0][0].tzinfo is not None


def test_baseload_ignores_export_and_weights_night():
    t = datetime(2026, 1, 10, 1, 0, tzinfo=UTC)  # 02:00 local -> night
    day = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
    minutes = [(t, 150.0)] * 60 + [(day, 400.0)] * 300 + [(day, -800.0)] * 300
    w, n = baseload_for(minutes)
    assert w == 150.0 and n == 360
    assert weighted_percentile([(1, 10), (2, 10), (3, 80)], 0.05) == 1
    assert weighted_percentile([(1, 1), (2, 1), (3, 98)], 0.05) == 3


def test_step_change_detected_once():
    s = [(date(2026, 1, 1) + timedelta(days=i), 150.0 if i < 20 else 200.0) for i in range(40)]
    steps = step_changes(s)
    assert len(steps) == 1 and steps[0]["day"] == date(2026, 1, 21)


def test_anomaly_threshold():
    hist = [1.0, 1.1, 0.9, 1.0, 1.05]
    assert anomaly(1.2, hist, 3, 0.2) is None
    assert anomaly(2.5, hist, 3, 0.2)["limit"] < 2.5
    assert anomaly(2.5, hist[:3], 3, 0.2) is None      # too little history
    assert anomaly(0.1, [0.01] * 8, 3, 0.2) is None     # below minimum


def test_quiet_hours_across_midnight():
    rule = {"quiet_start": time(22, 0), "quiet_end": time(7, 0)}
    assert in_quiet_hours(rule, datetime(2026, 1, 1, 22, 30, tzinfo=UTC))   # 23:30 local
    assert not in_quiet_hours(rule, datetime(2026, 1, 1, 12, 0, tzinfo=UTC))
    assert not in_quiet_hours({"quiet_start": None, "quiet_end": None}, datetime.now(UTC))
