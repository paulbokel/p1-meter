import asyncio

from p1collector.main import Collector
from p1collector.meter import MeterError
from p1collector.parse import parse_v1
from p1collector.store import Store


class HangingStore(Store):
    """A store whose database hangs (e.g. unreachable host without RST)."""

    def __init__(self):
        super().__init__("postgresql://unused")
        self.flush_calls = 0

    async def load_epoch_state(self):
        return 1, {}

    async def flush(self):
        self.flush_calls += 1
        await asyncio.sleep(3600)

    async def heartbeat(self, **fields):
        pass


class FakeDriver:
    name = "fake"

    def __init__(self, values):
        self.values = list(values)

    async def read(self):
        v = self.values.pop(0) if len(self.values) > 1 else self.values[0]
        if v is None:
            raise MeterError("down")
        return parse_v1({"total_power_import_t1_kwh": v, "active_power_w": 100})

    async def info(self):
        return {}

    async def close(self):
        pass


def test_polling_continues_while_database_hangs(monkeypatch):
    monkeypatch.setattr("p1collector.main.FLUSH_EVERY_S", 0.05)

    async def run():
        store = HangingStore()
        c = Collector(store, FakeDriver([1.0]), interval=0.05, host="x")
        task = asyncio.create_task(c.run())
        await asyncio.sleep(0.6)
        c.stop()
        task.cancel()
        return store

    store = asyncio.run(run())
    assert store.flush_calls >= 1
    assert len(store.buffer) >= 8  # ~12 polls in 0.6 s despite the hanging flush


def test_epoch_and_events_are_queued_not_written_inline():
    async def run():
        store = Store("postgresql://unused")
        c = Collector(store, FakeDriver([]), interval=1, host="x")
        await c.handle_reading(parse_v1({"total_power_import_t1_kwh": 10.0}))
        await c.handle_reading(parse_v1({"total_power_import_t1_kwh": 0.5}))   # meter swap
        for _ in range(10):
            await c.handle_failure(MeterError("down"))
        await c.handle_reading(parse_v1({"total_power_import_t1_kwh": 0.6}))
        return store

    store = asyncio.run(run())
    sqls = [sql.split("(")[0].strip() for sql, _ in store.pending]
    assert sqls == ["INSERT INTO meter_epoch", "INSERT INTO event", "INSERT INTO event", "INSERT INTO event"]
    kinds = [args[1] for sql, args in store.pending if "INTO event" in sql]
    assert kinds == ["meter_epoch", "meter_offline", "meter_online"]
    assert [r.epoch for r in store.buffer] == [1, 2, 2]
