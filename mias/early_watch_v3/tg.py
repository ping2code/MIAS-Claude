"""V3 Telegram: V3's OWN bot and chat, never the V1 / V2 bot or chats.

Token: env var TELEGRAM_BOT_TOKEN_V3 only. It comes from the service environment, or from the gitignored local file
``.env.v3`` (KEY=VALUE, read by V3 only, never overriding the real environment). The V1 / V2 bot token and the
V1 / V2 alert chats are never read here. With no V3 token (or no V3 chat), V3 keeps detecting and storing
and records "send skipped: no V3 token" on every Watch it would have sent.

The token is never logged, printed or stored: request URLs are not logged (httpx is at WARNING), errors carry only
Telegram's description or the exception type, and ``redact`` scrubs the token from any text before it is logged or
written.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx

TOKEN_ENV = "TELEGRAM_BOT_TOKEN_V3"
SECRETS_FILE = Path(".env.v3")
NO_TOKEN = "send skipped: no V3 token"
NO_CHAT = "send skipped: no V3 chat"
TEST_TEXT = "V3 EARLY WATCH · delivery test · not a Watch"


def load_secrets(path: Path = SECRETS_FILE) -> None:
    """Load TELEGRAM_BOT_TOKEN_V3 from the gitignored .env.v3 (only that key; the real environment wins)."""
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if line.startswith("export "):
            line = line[7:]
        key, sep, value = line.partition("=")
        if sep and key.strip() == TOKEN_ENV:
            os.environ.setdefault(TOKEN_ENV, value.strip().strip('"').strip("'"))


def token() -> str | None:
    t = os.environ.get(TOKEN_ENV, "").strip()
    return t or None


def redact(text, tok: str | None = None):
    tok = tok or token()
    if text is None or not tok:
        return text
    return str(text).replace(tok, "<V3 token>")


def make_sender(v3):
    """(sender, None) with V3's own token and chat, or (None, reason). Never a V1 / V2 token or chat."""
    tok = token()
    if not tok:
        return None, NO_TOKEN
    if not v3.chat_id:
        return None, NO_CHAT
    from ..observer.telegram import WatchSender

    class V3Sender(WatchSender):                # own instance: own pacing / rate-limit state
        def _gap(self, chat: str) -> float:
            return v3.tg.group_gap_s if str(chat).startswith("-") else v3.tg.private_gap_s

    return V3Sender(tok, [v3.chat_id], max_wait_s=v3.tg.max_wait_s), None


# ---- read-only bot checks (getMe / getChat / getUpdates); results never include the token ----------------

def _api(method: str, params: dict | None = None, tok: str | None = None, transport=None) -> dict:
    tok = tok or token()
    if not tok:
        return {"ok": False, "description": NO_TOKEN}
    try:
        with httpx.Client(timeout=10, transport=transport) as c:
            r = c.post(f"https://api.telegram.org/bot{tok}/{method}", json=params or {})
        try:
            return r.json()
        except ValueError:
            return {"ok": False, "description": f"HTTP {r.status_code}"}
    except httpx.HTTPError as exc:
        return {"ok": False, "description": f"network error: {type(exc).__name__}"}


def check_chat(chat_id: str | None, transport=None) -> dict:
    """getMe, then getChat on the configured chat; if that fails (not found / migrated), the chats seen in
    getUpdates (V3's own bot only) and any migrate_to_chat_id."""
    out: dict = {}
    me = _api("getMe", transport=transport)
    out["bot"] = (me.get("result") or {}).get("username") if me.get("ok") else None
    out["getMe_error"] = None if me.get("ok") else me.get("description")
    if not me.get("ok"):
        return out
    ch = _api("getChat", {"chat_id": chat_id}, transport=transport) if chat_id else {"ok": False,
                                                                                    "description": NO_CHAT}
    out["chat_ok"] = bool(ch.get("ok"))
    out["chat"] = {k: (ch.get("result") or {}).get(k) for k in ("id", "type", "title")} if ch.get("ok") else None
    out["chat_error"] = None if ch.get("ok") else ch.get("description")
    out["migrate_to_chat_id"] = (ch.get("parameters") or {}).get("migrate_to_chat_id")
    if not ch.get("ok"):
        up = _api("getUpdates", {"limit": 100, "timeout": 0}, transport=transport)
        seen = {}
        for u in up.get("result") or []:
            for k in ("message", "my_chat_member", "channel_post", "edited_message", "chat_member"):
                c = (u.get(k) or {}).get("chat")
                if c and str(c.get("id", "")).startswith("-"):
                    seen[c["id"]] = {"id": c["id"], "type": c.get("type"), "title": c.get("title")}
                m = (u.get(k) or {}).get("migrate_to_chat_id")
                if m:
                    out["migrate_to_chat_id"] = m
        out["updates_ok"] = bool(up.get("ok"))
        out["updates_error"] = None if up.get("ok") else up.get("description")
        out["chats_in_updates"] = list(seen.values())
    return out


class RedactFilter(logging.Filter):
    """Scrubs the V3 token from every log record (defence in depth; nothing logs it on purpose)."""

    def filter(self, record: logging.LogRecord) -> bool:
        tok = token()
        if tok:
            msg = record.getMessage()
            if tok in msg:
                record.msg, record.args = msg.replace(tok, "<V3 token>"), None
        return True


def install_log_redaction() -> None:
    f = RedactFilter()
    for h in logging.getLogger().handlers:
        h.addFilter(f)
