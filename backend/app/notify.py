"""Notification channels (F-ALERT-05). The user picks one or more:
web push (PWA), email (SMTP), ntfy, Telegram, Home Assistant notify, webhook."""
from __future__ import annotations

import asyncio
import json
import logging
import smtplib
import ssl
from email.message import EmailMessage

import httpx

log = logging.getLogger(__name__)

# config keys per kind, for validation and the settings UI
CHANNEL_FIELDS = {
    "webpush": [],
    "email": ["host", "port", "security", "username", "password", "from", "to"],
    "ntfy": ["url", "topic", "token", "priority"],
    "telegram": ["bot_token", "chat_id"],
    "homeassistant": ["url", "token", "service"],
    "webhook": ["url", "secret"],
}
SECRET_FIELDS = {"password", "token", "bot_token", "secret"}


def redact(kind: str, config: dict) -> dict:
    return {k: ("••••" if k in SECRET_FIELDS and v else v) for k, v in config.items()}


async def send(pool, client: httpx.AsyncClient, channel: dict, title: str, body: str, data: dict | None = None) -> None:
    kind, cfg = channel["kind"], channel["config"]
    if isinstance(cfg, str):
        cfg = json.loads(cfg)
    if kind == "ntfy":
        url = cfg.get("url", "https://ntfy.sh").rstrip("/") + "/" + cfg["topic"]
        headers = {"Title": title.encode("utf-8").decode("latin-1", "ignore"), "Tags": "zap"}
        if cfg.get("token"):
            headers["Authorization"] = f"Bearer {cfg['token']}"
        if cfg.get("priority"):
            headers["Priority"] = str(cfg["priority"])
        (await client.post(url, content=body.encode(), headers=headers, timeout=15)).raise_for_status()
    elif kind == "telegram":
        url = f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage"
        (await client.post(url, json={"chat_id": cfg["chat_id"], "text": f"{title}\n{body}"},
                           timeout=15)).raise_for_status()
    elif kind == "homeassistant":
        domain, _, service = cfg.get("service", "notify.notify").partition(".")
        url = cfg["url"].rstrip("/") + f"/api/services/{domain}/{service}"
        (await client.post(url, json={"title": title, "message": body},
                           headers={"Authorization": f"Bearer {cfg['token']}"}, timeout=15)).raise_for_status()
    elif kind == "webhook":
        headers = {"X-P1-Secret": cfg["secret"]} if cfg.get("secret") else {}
        (await client.post(cfg["url"], json={"title": title, "message": body, "data": data or {}},
                           headers=headers, timeout=15)).raise_for_status()
    elif kind == "email":
        await asyncio.to_thread(_send_email, cfg, title, body)
    elif kind == "webpush":
        await _send_webpush(pool, title, body, data)
    else:
        raise ValueError(f"unknown channel kind {kind}")


def _send_email(cfg: dict, title: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = cfg["from"]
    msg["To"] = cfg["to"]
    msg.set_content(body)
    port = int(cfg.get("port") or 587)
    security = cfg.get("security", "starttls")
    ctx = ssl.create_default_context()
    if security == "ssl":
        smtp = smtplib.SMTP_SSL(cfg["host"], port, timeout=20, context=ctx)
    else:
        smtp = smtplib.SMTP(cfg["host"], port, timeout=20)
    with smtp:
        if security == "starttls":
            smtp.starttls(context=ctx)
        if cfg.get("username"):
            smtp.login(cfg["username"], cfg.get("password", ""))
        smtp.send_message(msg)


async def vapid_keys(pool) -> dict:
    """VAPID key pair for web push, generated once and kept in settings."""
    row = await pool.fetchval("SELECT value FROM setting WHERE key = 'vapid'")
    if row:
        return json.loads(row) if isinstance(row, str) else row
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    der = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    priv = base64.urlsafe_b64encode(der).rstrip(b"=").decode()
    pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    keys = {"private": priv, "public": base64.urlsafe_b64encode(pub).rstrip(b"=").decode()}
    await pool.execute("INSERT INTO setting (key, value) VALUES ('vapid', $1::jsonb) ON CONFLICT (key) DO NOTHING",
                       json.dumps(keys))
    return await vapid_keys(pool)


async def _send_webpush(pool, title: str, body: str, data: dict | None) -> None:
    from pywebpush import WebPushException, webpush
    keys = await vapid_keys(pool)
    subs = await pool.fetch("SELECT id, endpoint, keys FROM push_subscription")
    if not subs:
        raise RuntimeError("no devices subscribed to web push")
    payload = json.dumps({"title": title, "body": body, "data": data or {}})
    errors = []
    for s in subs:
        info = {"endpoint": s["endpoint"], "keys": json.loads(s["keys"]) if isinstance(s["keys"], str) else s["keys"]}
        try:
            await asyncio.to_thread(webpush, info, payload, vapid_private_key=keys["private"],
                                    vapid_claims={"sub": "mailto:p1-monitor@localhost"}, timeout=15)
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):  # subscription gone
                await pool.execute("DELETE FROM push_subscription WHERE id = $1", s["id"])
            else:
                errors.append(str(e))
    if errors and len(errors) == len(subs):
        raise RuntimeError("; ".join(errors))
