"""Background jobs: alert rules (every minute), base load (daily + backfill),
day-ahead prices (hourly, only when a contract needs them). Run as
`python -m app.worker`; the compose stack has a separate `worker` service so
API restarts do not interrupt the jobs."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

import asyncpg
import httpx

from . import alerts, baseload, prices

log = logging.getLogger("worker")


async def record(pool, name: str, error: str | None) -> None:
    await pool.execute(
        "INSERT INTO job_status (name, last_run_at, last_ok_at, last_error) VALUES ($1, now(), "
        "CASE WHEN $2::text IS NULL THEN now() END, $2) ON CONFLICT (name) DO UPDATE SET last_run_at = now(), "
        "last_ok_at = CASE WHEN $2::text IS NULL THEN now() ELSE job_status.last_ok_at END, last_error = $2",
        name, error)


async def job(pool, name: str, fn, every_s: float) -> None:
    while True:
        try:
            result = await fn()
            if result:
                log.info("%s: %s", name, result)
            await record(pool, name, None)
        except Exception as e:
            log.exception("%s failed", name)
            try:
                await record(pool, name, f"{type(e).__name__}: {e}"[:500])
            except Exception:
                pass
        await asyncio.sleep(every_s)


async def needs_prices(pool) -> bool:
    return bool(await pool.fetchval(
        "SELECT EXISTS (SELECT 1 FROM tariff_contract WHERE (kind = 'dynamic' OR gas_dynamic) "
        "AND (valid_to IS NULL OR valid_to > current_date - 2))")) or \
        os.environ.get("FETCH_PRICES", "").lower() in ("1", "true", "yes")


async def backfill_quality(pool) -> str | None:
    """Once: materialise quality_1h for raw data that predates the migration
    (the refresh policy only looks back 3 days, raw data is kept 30)."""
    if await pool.fetchval("SELECT EXISTS (SELECT 1 FROM setting WHERE key = 'quality_backfilled')"):
        return None
    await pool.execute("CALL refresh_continuous_aggregate('quality_1h', NULL, now() - interval '1 hour')")
    await pool.execute("INSERT INTO setting (key, value) VALUES ('quality_backfilled', 'true'::jsonb) "
                       "ON CONFLICT (key) DO NOTHING")
    return "quality_1h backfilled"


async def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=4)
    client = httpx.AsyncClient()

    async def price_job():
        if await needs_prices(pool):
            return await prices.update_prices(pool, client)

    async def baseload_job():
        n = await baseload.backfill(pool)
        return f"computed {n} day(s)" if n else None

    async def alert_job():
        n = await alerts.run_rules(pool, client, datetime.now(timezone.utc))
        return f"{n} alert(s)" if n else None

    try:
        if msg := await backfill_quality(pool):
            log.info(msg)
    except Exception:
        log.exception("quality backfill failed")

    log.info("worker started")
    await asyncio.gather(
        job(pool, "prices", price_job, 3600),
        job(pool, "baseload", baseload_job, 1800),
        job(pool, "alerts", alert_job, 60),
    )


if __name__ == "__main__":
    asyncio.run(main())
