"""Drivers for the HomeWizard P1 meter: v1 REST polling and v2 WebSocket push."""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time

import httpx
import websockets

from .parse import Reading, meter_info_v1, meter_info_v2, parse_v1, parse_v2

log = logging.getLogger(__name__)


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


def lan_ssl_context(ca_file: str | None = None) -> ssl.SSLContext:
    """The meter's certificate is issued by HomeWizard for a serial-based name,
    not for its IP. Without a CA file only encryption is used; with the
    HomeWizard CA the chain is verified (the host name still cannot be)."""
    if ca_file:
        ctx = ssl.create_default_context(cafile=ca_file)
        ctx.check_hostname = False
        return ctx
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class V2WsDriver:
    """API v2 (firmware >= 6.00): one WebSocket, the meter pushes measurements.

    A background task keeps the connection (reconnecting with backoff) and
    stores the newest measurement; read() hands it to the polling loop, so the
    rest of the collector works the same as with v1.
    """
    name = "v2"
    STALE_S = 5.0

    def __init__(self, host: str, token: str | None, timeout: float = 2.0, ca_file: str | None = None):
        if not token:
            raise ValueError("driver v2 needs a token: pair the meter in Settings or run `python -m p1collector.pair`")
        self.url = host if host.startswith(("ws://", "wss://")) else f"wss://{host}/api/ws"
        self.token = token
        self.timeout = timeout
        self.ssl = lan_ssl_context(ca_file) if self.url.startswith("wss://") else None
        self.latest: dict | None = None
        self.latest_at = 0.0
        self.device: dict = {}
        self.error: str | None = None
        self._task: asyncio.Task | None = None

    async def _session(self) -> None:
        async with websockets.connect(self.url, ssl=self.ssl, open_timeout=self.timeout * 3,
                                      ping_interval=20, ping_timeout=20) as ws:
            authorized = False
            async for raw in ws:
                msg = json.loads(raw)
                kind, data = msg.get("type"), msg.get("data")
                if kind == "authorization_requested":
                    await ws.send(json.dumps({"type": "authorization", "data": self.token}))
                elif kind == "authorized":
                    authorized = True
                    self.error = None
                    log.info("v2 websocket authorized")
                    for topic in ("measurement", "device"):
                        await ws.send(json.dumps({"type": "subscribe", "data": topic}))
                elif kind == "measurement" and authorized:
                    self.latest, self.latest_at = data, time.monotonic()
                elif kind == "device":
                    self.device = data or {}
                elif kind == "error":
                    detail = data.get("message") if isinstance(data, dict) else data
                    self.error = f"meter error: {detail}"
                    if "unauthorized" in str(detail):
                        raise MeterError(self.error)

    async def _run(self) -> None:
        delay = 1
        while True:
            started = time.monotonic()
            try:
                await self._session()
                self.error = "websocket closed by meter"
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network, TLS, protocol: keep trying
                self.error = f"{type(e).__name__}: {e}"
            if time.monotonic() - started > 60:
                delay = 1
            log.warning("v2 websocket: %s; reconnecting in %ss", self.error, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def read(self) -> Reading:
        if self._task is None:
            self._task = asyncio.create_task(self._run())
        if self.latest is None or time.monotonic() - self.latest_at > self.STALE_S:
            raise MeterError(self.error or "no measurement received yet")
        return parse_v2(self.latest)

    async def info(self) -> dict:
        return meter_info_v2(self.latest, self.device)

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)


def make_driver(cfg, ca_file: str | None = None):
    if cfg.driver == "v1":
        return V1RestDriver(cfg.host, cfg.timeout)
    if cfg.driver == "v2":
        return V2WsDriver(cfg.host, cfg.token, cfg.timeout, ca_file)
    raise ValueError(f"unknown driver {cfg.driver!r} (supported: v1, v2)")
