"""Base load / standby consumption (F-BASE).

Per local day: the 5th percentile of 1-minute average *import* power, with
night minutes (00:00-06:00 local) counted three times so that the quiet hours
dominate. Minutes with net export (solar) say nothing about standby use and
are excluded, which is why daytime minutes in summer barely matter.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .costs import ZONE

METHOD = "p5_import_1m_night_weighted"
NIGHT_WEIGHT = 3
PERCENTILE = 0.05


def weighted_percentile(values: list[tuple[float, int]], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    total = sum(w for _, w in values)
    target = q * total
    acc = 0
    for v, w in values:
        acc += w
        if acc >= target:
            return v
    return values[-1][0]


def baseload_for(minutes: list[tuple[datetime, float]]) -> tuple[float | None, int]:
    vals = []
    for t, w in minutes:
        if w is None or w < 0:
            continue
        weight = NIGHT_WEIGHT if t.astimezone(ZONE).hour < 6 else 1
        vals.append((w, weight))
    return weighted_percentile(vals, PERCENTILE), len(vals)


async def compute_day(pool, day: date) -> dict | None:
    start = datetime(day.year, day.month, day.day, tzinfo=ZONE)
    end = start + timedelta(days=1)
    rows = await pool.fetch("SELECT bucket, power_avg_w FROM reading_1m WHERE bucket >= $1 AND bucket < $2",
                            start, end)
    if not rows:
        return None
    watts, samples = baseload_for([(r["bucket"], r["power_avg_w"]) for r in rows])
    minutes_in_day = int((end.astimezone(ZONE) - start).total_seconds() // 60)
    confidence = min(1.0, len(rows) / minutes_in_day)
    await pool.execute(
        "INSERT INTO baseload_1d (day, watts, method, confidence, samples) VALUES ($1, $2, $3, $4, $5) "
        "ON CONFLICT (day) DO UPDATE SET watts = EXCLUDED.watts, method = EXCLUDED.method, "
        "confidence = EXCLUDED.confidence, samples = EXCLUDED.samples, computed_at = now()",
        day, watts, METHOD, confidence, samples)
    return {"day": day, "watts": watts, "confidence": confidence, "samples": samples}


async def backfill(pool, max_days: int = 730) -> int:
    """Compute every finished day that has 1-minute data but no base load yet."""
    today = datetime.now(ZONE).date()
    rows = await pool.fetch(
        "SELECT DISTINCT (bucket AT TIME ZONE 'Europe/Amsterdam')::date AS day FROM reading_1m "
        "WHERE bucket > now() - make_interval(days => $1) "
        "EXCEPT SELECT day FROM baseload_1d WHERE confidence > 0.95 ORDER BY 1", max_days)
    n = 0
    for r in rows:
        if r["day"] < today:
            await compute_day(pool, r["day"])
            n += 1
    return n


def step_changes(series: list[tuple[date, float]], window: int = 7, threshold_w: float = 25) -> list[dict]:
    """Mark days where the median of the next `window` days differs from the
    previous `window` days by more than threshold (F-BASE-03)."""
    from statistics import fmean, median
    out = []
    vals = [(d, w) for d, w in series if w is not None]
    i = window
    while i <= len(vals) - window:
        before = median(w for _, w in vals[i - window:i])
        after = median(w for _, w in vals[i:i + window])
        if abs(after - before) >= threshold_w:
            # place the marker where the difference peaks within the next window
            def diff(j):  # means peak exactly at the split, medians plateau
                return abs(fmean(w for _, w in vals[j:j + window]) - fmean(w for _, w in vals[j - window:j]))
            j = max(range(i, min(i + window, len(vals) - window + 1)), key=diff)
            out.append({"day": vals[j][0], "before_w": median(w for _, w in vals[j - window:j]),
                        "after_w": median(w for _, w in vals[j:j + window])})
            i = j + window  # one marker per change
        else:
            i += 1
    return out
