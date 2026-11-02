"""Meter configuration: environment defaults, overridden by the `meter`
setting that admins edit in the web app (F-SET-01)."""
from __future__ import annotations

import re
from dataclasses import dataclass

DRIVERS = ("v1", "v2")
HOST_RE = re.compile(r"^[A-Za-z0-9.\-:\[\]]{1,253}$")


@dataclass(frozen=True)
class MeterConfig:
    host: str = "192.168.178.187"
    driver: str = "v1"
    interval: float = 1.0
    token: str | None = None
    timeout: float = 2.0

    @classmethod
    def from_env(cls, env) -> "MeterConfig":
        return cls(host=env.get("P1_HOST", cls.host), driver=env.get("P1_DRIVER", cls.driver),
                   interval=float(env.get("P1_POLL_INTERVAL", cls.interval)),
                   token=env.get("P1_TOKEN") or None, timeout=float(env.get("P1_TIMEOUT", cls.timeout)))

    def merged(self, setting: dict | None) -> "MeterConfig":
        """Apply the DB setting on top; invalid values are ignored."""
        s = setting or {}
        host = s.get("host") if isinstance(s.get("host"), str) and HOST_RE.match(s["host"]) else self.host
        driver = s.get("driver") if s.get("driver") in DRIVERS else self.driver
        try:
            interval = float(s["interval"]) if s.get("interval") is not None else self.interval
        except (TypeError, ValueError):
            interval = self.interval
        token = s.get("token") if isinstance(s.get("token"), str) and s["token"] else self.token
        # HomeWizard advises not to poll faster than every 500 ms.
        return MeterConfig(host=host, driver=driver, interval=max(0.5, min(interval, 60.0)), token=token,
                           timeout=self.timeout)
