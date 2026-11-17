"""[observer] settings (operational only; the Early Watch rules are frozen in rules.py)."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import time
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    enabled: bool
    symbols: list[str]
    ws_url: str
    db_path: Path
    cache_dir: Path
    minute_grace_s: float
    watchdog_s: float
    connect_from: time
    connect_until: time
    confirm_poll_s: float
    telegram: bool          # pilot: send the initial message of fresh main-lane watches (user, 2026-11-11)
    telegram_max_age_s: float = 180   # a watch older than this when found (e.g. restart backfill) is not sent
    v2: bool = False                  # Early Watch v2 research families (docs/observer/V2_PREREGISTRATION.md)
    v2_db_path: Path = Path("data/observer/v2.db")
    v2_telegram: bool = False
    v2_telegram_muted: tuple[str, ...] = ()   # v2 families detected / stored / scored but never sent alone (2026-11-16)


def load(path: str | Path = "config.toml", fallback_symbols: list[str] | None = None) -> Settings:
    with open(path, "rb") as fh:
        raw = tomllib.load(fh).get("observer", {})
    syms = [str(s).upper() for s in raw.get("symbols") or fallback_symbols or []]
    if "SPY" not in syms:
        syms = ["SPY"] + syms          # SPY context is read from the observer's own SPY state
    syms = ["SPY"] + [s for s in syms if s != "SPY"]
    return Settings(
        enabled=bool(raw.get("enabled", False)), symbols=syms,
        ws_url=str(raw.get("ws_url", "wss://socket.massive.com/stocks")),
        db_path=Path(raw.get("db_path", "data/observer/observer.db")),
        cache_dir=Path(raw.get("cache_dir", "data/observer/cache")),
        minute_grace_s=float(raw.get("minute_grace_seconds", 5)),
        watchdog_s=float(raw.get("watchdog_seconds", 150)),
        connect_from=time.fromisoformat(str(raw.get("connect_from", "04:00"))),
        connect_until=time.fromisoformat(str(raw.get("connect_until", "20:05"))),
        confirm_poll_s=float(raw.get("confirm_poll_seconds", 20)),
        telegram=bool(raw.get("telegram", False)),
        telegram_max_age_s=float(raw.get("telegram_max_age_seconds", 180)),
        v2=bool(raw.get("v2", False)), v2_db_path=Path(raw.get("v2_db_path", "data/observer/v2.db")),
        v2_telegram=bool(raw.get("v2_telegram", False)),
        v2_telegram_muted=tuple(str(f).lower() for f in raw.get("v2_telegram_muted_families") or ()))
