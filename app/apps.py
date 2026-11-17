"""Connected apps: send an answer or a generated file (PDF, Word, Excel) to another app.

Supported: Slack, Discord, Telegram, email (SMTP) and any webhook (Zapier, n8n, Make, your own system).
Secrets (webhook URLs, bot tokens, passwords) are stored encrypted and are never sent back to the browser.

Safety: this server makes the outgoing call, so addresses are checked first. Only https is accepted, names that
point to private / local / internal network addresses are refused (set ALLOW_PRIVATE_APP_URLS=true to connect a
self-hosted tool such as n8n on your own network), redirects are not followed, and Slack / Discord / Telegram
addresses must belong to those services.
"""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import re
import smtplib
import socket
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from urllib.parse import urlparse

import httpx

from .config import cfg
from .security import decrypt

TIMEOUT = httpx.Timeout(20.0, connect=8.0)
MAX_FILE_FOR_DISCORD = 8 * 1024 * 1024
MAX_FILE_FOR_EMAIL = 10 * 1024 * 1024
MAX_FILE_FOR_WEBHOOK = 2 * 1024 * 1024
MAX_APPS = 50

# field: key, label, secret (stored encrypted), optional, kind (text|bool|select), hint, options
TYPES: dict[str, dict] = {
    "webhook": {
        "label": "Webhook (Zapier, n8n, Make, your own system)",
        "help": "Sends a JSON message to any web address. In Zapier use “Webhooks by Zapier → Catch Hook”, in n8n a Webhook node.",
        "fields": [
            {"key": "url", "label": "Webhook address (https://…)", "secret": True},
            {"key": "secret", "label": "Secret (sent as the X-Webhook-Secret header)", "secret": True, "optional": True},
            {"key": "include_file", "label": "Include the file itself (base64, up to 2 MB)", "kind": "bool", "optional": True},
        ],
    },
    "slack": {
        "label": "Slack",
        "help": "In Slack create an Incoming Webhook for a channel and paste its address. Slack webhooks carry text only: file name and size are included.",
        "fields": [{"key": "url", "label": "Incoming webhook address (https://hooks.slack.com/…)", "secret": True}],
    },
    "discord": {
        "label": "Discord",
        "help": "Channel settings → Integrations → Webhooks → Copy webhook address. Files up to 8 MB are attached.",
        "fields": [{"key": "url", "label": "Webhook address (https://discord.com/api/webhooks/…)", "secret": True}],
    },
    "telegram": {
        "label": "Telegram",
        "help": "Create a bot with @BotFather, send it a message once, then enter its token and your chat id. Files are attached.",
        "fields": [
            {"key": "bot_token", "label": "Bot token", "secret": True},
            {"key": "chat_id", "label": "Chat id"},
        ],
    },
    "email": {
        "label": "Email (SMTP)",
        "help": "Any SMTP account (Gmail needs an app password). The file is attached (up to 10 MB).",
        "fields": [
            {"key": "host", "label": "SMTP server (e.g. smtp.gmail.com)"},
            {"key": "port", "label": "Port (587 for STARTTLS, 465 for SSL)"},
            {"key": "security", "label": "Security", "kind": "select", "options": ["starttls", "ssl", "none"]},
            {"key": "username", "label": "Username", "optional": True},
            {"key": "password", "label": "Password / app password", "secret": True, "optional": True},
            {"key": "sender", "label": "From address"},
            {"key": "to", "label": "Send to (comma separated, up to 10)"},
        ],
    },
}


class AppError(Exception):
    """A problem to show to the user (never contains a secret)."""


def split_config(type_: str, config: dict, existing_plain: dict | None = None, existing_secret: dict | None = None
                 ) -> tuple[dict, dict]:
    """Validates a form and splits it into (plain fields, secret fields). Blank secret = keep the stored one."""
    spec = TYPES.get(type_)
    if not spec:
        raise AppError("Unknown app type")
    plain, secret = dict(existing_plain or {}), dict(existing_secret or {})
    for f in spec["fields"]:
        k = f["key"]
        raw = config.get(k)
        if f.get("kind") == "bool":
            plain[k] = bool(raw) if raw is not None else bool(plain.get(k, False))
            continue
        val = ("" if raw is None else str(raw)).strip()
        target = secret if f.get("secret") else plain
        if val:
            target[k] = val
        elif f.get("secret") and k in secret:
            pass  # keep the stored secret
        elif not f.get("optional"):
            raise AppError(f"“{f['label']}” is required")
        else:
            target.pop(k, None)
        if f.get("kind") == "select" and target.get(k) not in f["options"]:
            raise AppError(f"“{f['label']}” must be one of: {', '.join(f['options'])}")
    _check_shape(type_, {**plain, **secret})
    return plain, secret


_SLACK_HOSTS = ("hooks.slack.com",)
_DISCORD_HOSTS = ("discord.com", "discordapp.com")


def _check_shape(type_: str, c: dict) -> None:
    """Checks that do not need the network."""
    if type_ in ("webhook", "slack", "discord"):
        _parse_url(c["url"], {"slack": _SLACK_HOSTS, "discord": _DISCORD_HOSTS}.get(type_))
    elif type_ == "telegram":
        if not re.fullmatch(r"\d{5,}:[\w-]{20,}", c["bot_token"]):
            raise AppError("That does not look like a Telegram bot token (12345:ABC…)")
        if not re.fullmatch(r"-?\d+|@\w{4,}", c["chat_id"]):
            raise AppError("Chat id is a number (or @channelname)")
    elif type_ == "email":
        if not re.fullmatch(r"[\w.-]+", c["host"]) or not c["port"].isdigit() or not 0 < int(c["port"]) < 65536:
            raise AppError("Check the SMTP server and port")
        if c.get("username") and c["security"] == "none":
            raise AppError("Use STARTTLS or SSL when a password is set")
        _addresses(c["sender"], 1)
        _addresses(c["to"], 10)


def _addresses(raw: str, limit: int) -> list[str]:
    out = [a.strip() for a in raw.split(",") if a.strip()]
    if not out or len(out) > limit or not all(re.fullmatch(r"[^@\s,;<>\"]+@[^@\s,;<>\"]+\.[^@\s,;<>\"]+", a) for a in out):
        raise AppError("Enter valid email address(es)" + (f" (at most {limit})" if limit > 1 else ""))
    return out


def _parse_url(url: str, hosts: tuple[str, ...] | None = None):
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise AppError("Enter a full web address starting with https://")
    if u.scheme == "http" and not cfg.allow_private_app_urls:
        raise AppError("Use an https:// address (http is only allowed with ALLOW_PRIVATE_APP_URLS=true)")
    if u.username or u.password:
        raise AppError("Do not put a user name or password in the address")
    host = u.hostname.lower()
    if hosts and not any(host == h or host.endswith("." + h) for h in hosts):
        raise AppError(f"That address must belong to {hosts[0]}")
    return u


def _public_ip(ip: str) -> bool:
    a = ipaddress.ip_address(ip)
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return not (a.is_private or a.is_loopback or a.is_link_local or a.is_reserved or a.is_multicast or a.is_unspecified)


async def _guard_host(host: str, port: int) -> None:
    """Refuses names that resolve to this machine or to private networks (server-side request forgery)."""
    if cfg.allow_private_app_urls:
        return
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
    except socket.gaierror:
        raise AppError("That address cannot be found (check the spelling)")
    if not infos or not all(_public_ip(i[4][0]) for i in infos):
        raise AppError("That address points to a private or local network, which is not allowed "
                       "(set ALLOW_PRIVATE_APP_URLS=true to connect a tool on your own network)")


def _kb(n: int) -> str:
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{max(1, round(n / 1024))} KB"


async def _post(client: httpx.AsyncClient, url: str, **kw) -> httpx.Response:
    try:
        r = await client.post(url, **kw)
    except httpx.TimeoutException:
        raise AppError("The app did not answer in time")
    except httpx.HTTPError as e:
        raise AppError(f"Could not reach the app ({type(e).__name__})")
    return r


def _ok(r: httpx.Response, what: str) -> None:
    if r.status_code >= 300:
        hint = " The address or secret may be wrong or revoked." if r.status_code in (401, 403, 404) else ""
        raise AppError(f"{what} answered HTTP {r.status_code}.{hint}")


async def send(app: dict, text: str, file: dict | None = None) -> str:
    """Delivers a message (and optionally a stored file) to the app. Returns a short confirmation."""
    c = {**(app.get("config") or {}), **{k: decrypt(v) for k, v in (app.get("secrets_enc") or {}).items()}}
    type_ = app["type"]
    text = (text or "").strip()[:3500]
    data = bytes(file["data"]) if file else None
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
        if type_ in ("webhook", "slack", "discord"):
            u = _parse_url(c["url"], {"slack": _SLACK_HOSTS, "discord": _DISCORD_HOSTS}.get(type_))
            await _guard_host(u.hostname, u.port or (443 if u.scheme == "https" else 80))
            if type_ == "webhook":
                body = {"event": "message", "source": "ask-my-data", "text": text,
                        "sent_at": datetime.now(timezone.utc).isoformat()}
                if file:
                    body["file"] = {"name": file["name"], "mime": file["mime"], "size": file["size"]}
                    if c.get("include_file") and len(data) <= MAX_FILE_FOR_WEBHOOK:
                        body["file"]["base64"] = base64.b64encode(data).decode()
                headers = {"X-Webhook-Secret": c["secret"]} if c.get("secret") else {}
                _ok(await _post(client, c["url"], json=body, headers=headers), "The webhook")
            elif type_ == "slack":
                msg = text + (f"\n📎 {file['name']} ({_kb(file['size'])})" if file else "")
                _ok(await _post(client, c["url"], json={"text": msg or "(empty message)"}), "Slack")
            else:
                content = (text or (file["name"] if file else ""))[:1900]
                if file and len(data) <= MAX_FILE_FOR_DISCORD:
                    _ok(await _post(client, c["url"], data={"payload_json": json.dumps({"content": content})},
                                    files={"files[0]": (file["name"], data, file["mime"])}), "Discord")
                else:
                    extra = f"\n📎 {file['name']} ({_kb(file['size'])}, too large to attach)" if file else ""
                    _ok(await _post(client, c["url"], json={"content": (content + extra)[:1990]}), "Discord")
        elif type_ == "telegram":
            base = f"https://api.telegram.org/bot{c['bot_token']}"
            if file:
                r = await _post(client, base + "/sendDocument", data={"chat_id": c["chat_id"], "caption": text[:1000]},
                                files={"document": (file["name"], data, file["mime"])})
            else:
                r = await _post(client, base + "/sendMessage", json={"chat_id": c["chat_id"], "text": text or "(empty message)"})
            if r.status_code >= 300:
                try:
                    why = str(r.json().get("description", ""))[:120]
                except ValueError:
                    why = ""
                raise AppError(f"Telegram answered HTTP {r.status_code}. {why}".strip())
        elif type_ == "email":
            await _guard_host(c["host"], int(c["port"]))
            await asyncio.to_thread(_send_email, c, text, file, data)
        else:
            raise AppError("Unknown app type")
    return "Sent"


def _send_email(c: dict, text: str, file: dict | None, data: bytes | None) -> None:
    msg = EmailMessage()
    first = (text.splitlines()[0] if text else (file["name"] if file else "Message")).strip()
    msg["Subject"] = re.sub(r"[\r\n]+", " ", first)[:120] or "Message from Ask my data"
    msg["From"] = _addresses(c["sender"], 1)[0]
    msg["To"] = ", ".join(_addresses(c["to"], 10))
    msg.set_content(text or "Please find the attached file.")
    if file:
        if len(data) > MAX_FILE_FOR_EMAIL:
            raise AppError(f"The file is {_kb(len(data))}; email attachments are limited to 10 MB")
        main, _, sub = (file["mime"] or "application/octet-stream").partition("/")
        msg.add_attachment(data, maintype=main, subtype=sub or "octet-stream", filename=file["name"])
    try:
        if c["security"] == "ssl":
            server = smtplib.SMTP_SSL(c["host"], int(c["port"]), timeout=20, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(c["host"], int(c["port"]), timeout=20)
        with server:
            if c["security"] == "starttls":
                server.starttls(context=ssl.create_default_context())
            if c.get("username"):
                server.login(c["username"], c.get("password", ""))
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise AppError("The mail server refused the user name or password")
    except (smtplib.SMTPException, OSError) as e:
        raise AppError(f"Could not send the email ({type(e).__name__})")


def mask(value: str) -> str:
    """Shows where a secret points without revealing it."""
    if value.startswith(("http://", "https://")):
        u = urlparse(value)
        return f"{u.scheme}://{u.hostname}/…"
    return "••••" + value[-3:] if len(value) > 8 else "••••"


def public(doc: dict) -> dict:
    secrets = {k: mask(decrypt(v)) for k, v in (doc.get("secrets_enc") or {}).items()}
    return {"id": doc["_id"], "name": doc["name"], "type": doc["type"], "enabled": doc.get("enabled", True),
            "config": doc.get("config") or {}, "secrets": secrets, "last_used": doc.get("last_used"),
            "last_error": doc.get("last_error")}
