"""Drivers for the HomeWizard P1 meter. Phase 1 ships the v1 REST driver."""
from __future__ import annotations

import httpx

from .parse import Reading, meter_info_v1, parse_v1


class MeterError(Exception):
    pass


class V1RestDriver:
    name = "v1"

    def __init__(self, host: str, timeout: float = 2.0):
        self.base = host if host.startswith("http") else f"http://{host}"
        self.client = httpx.AsyncClient(timeout=timeout)
        self._last_data: dict = {}

    async def _get(self, path: str) -> dict:
        try:
            resp = await self.client.get(self.base + path)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise MeterError(f"{type(e).__name__}: {e}") from e

    async def read(self) -> Reading:
        data = await self._get("/api/v1/data")
        self._last_data = data
        return parse_v1(data)

    async def info(self) -> dict:
        device = {}
        try:
            device = await self._get("/api")
        except MeterError:
            pass
        return meter_info_v1(self._last_data, device)

    async def close(self) -> None:
        await self.client.aclose()


def make_driver(name: str, host: str, timeout: float):
    if name == "v1":
        return V1RestDriver(host, timeout)
    raise ValueError(f"unknown driver {name!r} (phase 1 supports: v1)")
