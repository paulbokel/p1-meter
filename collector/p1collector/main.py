"""Collector: polls the P1 meter and writes every reading to TimescaleDB.

The collector is the only component that talks to the meter. It keeps
readings in memory while the database is unavailable and flushes them later.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from dataclasses import replace
from datetime import datetime, timezone

from .config import MeterConfig
from .meter import MeterError, make_driver
from .parse import EpochTracker, Reading
from .store import Store

log = logging.getLogger("p1collector")

OFFLINE_AFTER_FAILURES = 10
FLUSH_EVERY_S = 5
INFO_EVERY_S = 600
CONFIG_EVERY_S = 30


class Collector:
    def __init__(self, store: Store, driver, interval: float, host: str, setting: dict | None = None):
        self.store = store
        self.setting = setting  # the `meter` setting this run started with
        self.driver = driver
        self.interval = interval
        self.host = host
        self.failures = 0
        self.online = False
        self.last_reading_at: datetime | None = None
        self.last_error: str | None = None
        self.epochs = EpochTracker()
        self.last_fail_counts: tuple | None = None
        self.last_quality: dict[str, int] = {}
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def handle_reading(self, r: Reading) -> None:
        if self.epochs.check(r):
            msg = f"Energy counter decreased; starting meter epoch {self.epochs.epoch}"
            log.warning(msg)
            self.store.new_epoch(self.epochs.epoch, "counter decreased")
            self.store.event("meter_epoch", msg, "warning", {"epoch": self.epochs.epoch})

        counts = (r.power_fail_count, r.long_power_fail_count)
        if self.last_fail_counts and None not in counts and None not in self.last_fail_counts:
            if counts[0] > self.last_fail_counts[0] or counts[1] > self.last_fail_counts[1]:
                self.store.event("power_failure", "Power failure counter increased", "warning",
                                       {"any": counts[0], "long": counts[1]})
        self.last_fail_counts = counts

        for kind, prefix in (("voltage_sag", "sag"), ("voltage_swell", "swell")):
            for i in (1, 2, 3):
                attr = f"{prefix}_l{i}_count"
                v, prev = getattr(r, attr), self.last_quality.get(attr)
                if v is not None and prev is not None and v > prev:
                    self.store.event(kind, f"Voltage {prefix} on L{i}", "info", {"phase": f"L{i}", "count": v})
                if v is not None:
                    self.last_quality[attr] = v

        self.store.add(r)
        self.last_reading_at = r.ts
        self.last_error = None
        if not self.online:
            self.online = True
            if self.failures >= OFFLINE_AFTER_FAILURES:
                self.store.event("meter_online", "P1 meter is reachable again")
        self.failures = 0

    async def handle_failure(self, err: Exception) -> None:
        self.failures += 1
        self.last_error = str(err)
        if self.failures == OFFLINE_AFTER_FAILURES:
            self.online = False
            log.error("meter offline after %d failures: %s", self.failures, err)
            self.store.event("meter_offline", f"P1 meter unreachable: {err}", "error",
                                   {"failures": self.failures})
        elif self.failures < OFFLINE_AFTER_FAILURES:
            log.warning("read failed (%d): %s", self.failures, err)

    async def run(self) -> None:
        epoch, last = await self._retry(self.store.load_epoch_state)
        self.epochs = EpochTracker(epoch, last)
        await self.store.heartbeat(started_at=datetime.now(timezone.utc), driver=self.driver.name,
                                   meter_host=self.host)
        log.info("collector started: host=%s driver=%s interval=%ss epoch=%d",
                 self.host, self.driver.name, self.interval, epoch)

        writer = asyncio.create_task(self._writer())
        try:
            await self._poll()
        finally:
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
        log.info("stopping; flushing %d buffered readings", len(self.store.buffer))
        await self.store.flush()
        await self.driver.close()

    async def _poll(self) -> None:
        """Read the meter on a fixed cadence. Never waits on the database."""
        next_tick = time.monotonic()
        while not self._stop.is_set():
            try:
                await self.handle_reading(await self.driver.read())
            except MeterError as e:
                await self.handle_failure(e)

            next_tick += self.interval
            delay = next_tick - time.monotonic()
            if delay < 0:  # fell behind (slow meter); don't burst to catch up
                next_tick = time.monotonic()
                delay = 0
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _writer(self) -> None:
        """Flush buffered readings/events and write the heartbeat every few seconds."""
        last_info: float | None = None
        last_config = time.monotonic()
        while True:
            await asyncio.sleep(FLUSH_EVERY_S)
            await self.store.flush()
            hb = dict(online=self.online, last_reading_at=self.last_reading_at,
                      consecutive_failures=self.failures, last_error=self.last_error)
            now = time.monotonic()
            if self.online and (last_info is None or now - last_info >= INFO_EVERY_S):
                last_info = now
                hb["meter_info"] = await self.driver.info()
            await self.store.heartbeat(**hb)
            if now - last_config >= CONFIG_EVERY_S and hasattr(self.store, "get_setting"):
                last_config = now
                await self._check_config()

    async def _check_config(self) -> None:
        """Meter settings changed in the web app: stop, so the container restarts with them."""
        try:
            current = await self.store.get_setting("meter")
        except Exception:
            return
        if current != self.setting:
            log.info("meter settings changed; restarting collector")
            self.stop()

    async def _retry(self, fn):
        return await retry(self.store, fn)


async def retry(store, fn):
    delay = 1
    while True:
        try:
            return await fn()
        except Exception as e:  # DB not ready yet
            log.warning("waiting for database: %s", e)
            await store._reset_pool()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    store = Store(os.environ["DATABASE_URL"])

    async def runner():
        setting = await retry(store, lambda: store.get_setting("meter"))
        cfg = MeterConfig.from_env(os.environ).merged(setting)
        try:
            driver = make_driver(cfg, os.environ.get("P1_CA_FILE") or None)
        except ValueError as e:
            log.error("%s; falling back to driver v1", e)
            store.event("config_error", f"Meter driver {cfg.driver}: {e}; using v1", "error")
            driver = make_driver(replace(cfg, driver="v1"))
        collector = Collector(store, driver, cfg.interval, cfg.host, setting)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, collector.stop)
        await collector.run()

    asyncio.run(runner())


if __name__ == "__main__":
    main()
