"""Pair with a P1 meter on API v2 and print the token.

    python -m p1collector.pair 192.168.178.187

Press the button on the meter when asked. Then either save the token in the
web app (Settings → Meter) or put it in .env as P1_TOKEN with P1_DRIVER=v2.
"""
from __future__ import annotations

import sys
import time

import httpx

NAME = "local/p1monitor"


def request_token(client: httpx.Client, host: str) -> str | None:
    """One pairing attempt: the token, or None while the button was not pressed."""
    base = host if host.startswith("http") else f"https://{host}"
    r = client.post(f"{base}/api/user", json={"name": NAME}, headers={"X-Api-Version": "2"})
    if r.status_code == 403:
        return None
    r.raise_for_status()
    return r.json()["token"]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    print("Press the button on the P1 meter (you have 30 seconds)...")
    with httpx.Client(verify=False, timeout=5) as client:  # noqa: S501 - LAN device, HomeWizard-issued cert
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            token = request_token(client, argv[1])
            if token:
                print(f"P1_TOKEN={token}")
                return 0
            time.sleep(2)
    print("No button press seen. Is the firmware >= 6.00 (API v2)?", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
