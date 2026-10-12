"""Configuration: config.toml for behaviour, environment for secrets."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo


class ConfigError(Exception):
    pass


TIMEFRAMES = {"5m", "15m", "1h", "1d", "1w"}
DEFAULT_PROFILES = {
    "swing": {"1w": 0.35, "1d": 0.45, "1h": 0.20},
    "intraday": {"5m": 0.25, "15m": 0.30, "1h": 0.30, "1d": 0.15},
}


@dataclass(frozen=True)
class IdeaTemplate:
    """Which contract to show for a trading style."""
    min_dte: int
    max_dte: int
    delta: float
    max_spread_pct: float
    min_oi: float


DEFAULT_IDEAS = {
    "swing": IdeaTemplate(50, 100, 0.60, 10.0, 100),
    "day": IdeaTemplate(1, 14, 0.45, 5.0, 200),
}


def _templates(raw: dict) -> dict[str, IdeaTemplate]:
    out = dict(DEFAULT_IDEAS)
    for name, t in raw.items():
        base = DEFAULT_IDEAS.get(name, DEFAULT_IDEAS["swing"])
        tmpl = IdeaTemplate(int(t.get("min_dte", base.min_dte)), int(t.get("max_dte", base.max_dte)),
                            float(t.get("delta", base.delta)), float(t.get("max_spread_pct", base.max_spread_pct)),
                            float(t.get("min_oi", base.min_oi)))
        if not (0 <= tmpl.min_dte <= tmpl.max_dte) or not 0 < tmpl.delta < 1:
            raise ConfigError(f"options.ideas.{name} is invalid")
        out[name] = tmpl
    return out


@dataclass(frozen=True)
class Config:
    symbols: list[str]
    market_symbols: list[str]
    interval_seconds: int
    tz: ZoneInfo
    digest_times: list[str]
    base_url: str
    stock_delay_minutes: int
    max_concurrency: int
    request_timeout: float
    max_retries: int
    chain_max_dte: int
    chain_strike_pct: float
    max_chain_pages: int
    trend_profiles: dict[str, dict[str, float]]
    trend_primary: str
    trend_strong: float
    trend_bias: float
    block_premium: float
    unusual_min_premium: float
    unusual_vol_oi_ratio: float
    alert_0dte: bool
    idea_templates: dict[str, IdeaTemplate]
    thin_options_volume: float
    recent_windows: int
    options_strong: float
    options_bias: float
    confirm_cycles: int
    cooldown_minutes: int
    divergence_min_options_score: float
    iv_spike_points: float
    iv_spike_window_minutes: int
    block_cooldown_minutes: int
    telegram_enabled: bool
    telegram_commands: bool
    web_enabled: bool
    screen_enabled: bool
    screen_interval_minutes: int
    web_host: str
    web_port: int
    db_path: Path
    retention_days: int
    api_key: str = field(repr=False, default="")
    telegram_token: str = field(repr=False, default="")
    telegram_chat_id: str = field(repr=False, default="")


def load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, optional 'export', never overrides the real environment."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def load(path: str | Path = "config.toml", *, require_secrets: bool = True) -> Config:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    load_dotenv(path.parent / ".env")
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    def sec(name: str) -> dict:
        return raw.get(name, {})

    wl, sch, data, tr, opt, al, tg, web, st, scr = (sec(n) for n in (
        "watchlist", "schedule", "data", "trend", "options", "alerts", "telegram", "web", "storage", "screen"))

    symbols = [s.strip().upper() for s in os.environ.get("MIAS_SYMBOLS", "").split(",") if s.strip()] \
        or [s.upper() for s in wl.get("symbols", [])]
    if not symbols:
        raise ConfigError("watchlist.symbols is empty")
    if len(set(symbols)) != len(symbols):
        raise ConfigError("watchlist.symbols contains duplicates")

    profiles = {name: {k: float(v) for k, v in w.items()}
                for name, w in tr.get("profiles", DEFAULT_PROFILES).items()}
    for name, w in profiles.items():
        unknown = set(w) - TIMEFRAMES
        if unknown or not w:
            raise ConfigError(f"trend.profiles.{name} must use {sorted(TIMEFRAMES)}, got {sorted(w)}")
    primary = tr.get("profile", "swing")
    if primary not in profiles:
        raise ConfigError(f"trend.profile {primary!r} is not one of {sorted(profiles)}")

    interval = int(os.environ.get("MIAS_INTERVAL_SECONDS", sch.get("interval_seconds", 150)))
    if interval < 30:
        raise ConfigError("schedule.interval_seconds must be >= 30")

    db_path = Path(os.environ.get("MIAS_DB_PATH", st.get("path", "data/mias.db")))
    if not db_path.is_absolute():
        db_path = path.parent / db_path

    cfg = Config(
        symbols=symbols,
        market_symbols=[s.upper() for s in wl.get("market_symbols", ["SPY", "QQQ", "IWM"])],
        interval_seconds=interval,
        tz=ZoneInfo(sch.get("timezone", "America/New_York")),
        digest_times=list(sch.get("digest_times", [])),
        base_url=data.get("base_url", "https://api.massive.com").rstrip("/"),
        stock_delay_minutes=int(os.environ.get("MIAS_STOCK_DELAY_MINUTES", data.get("stock_delay_minutes", 15))),
        max_concurrency=int(data.get("max_concurrency", 8)),
        request_timeout=float(data.get("request_timeout_seconds", 20)),
        max_retries=int(data.get("max_retries", 4)),
        chain_max_dte=int(data.get("chain_max_dte", 45)),
        chain_strike_pct=float(data.get("chain_strike_pct", 0.15)),
        max_chain_pages=int(data.get("max_chain_pages", 40)),
        trend_profiles=profiles,
        trend_primary=primary,
        trend_strong=float(tr.get("strong_threshold", 60)),
        trend_bias=float(tr.get("bias_threshold", 25)),
        block_premium=float(opt.get("block_premium", 1_000_000)),
        unusual_min_premium=float(opt.get("unusual_min_premium", 500_000)),
        unusual_vol_oi_ratio=float(opt.get("unusual_vol_oi_ratio", 1.0)),
        alert_0dte=bool(opt.get("alert_0dte", False)),
        idea_templates=_templates(opt.get("ideas", {})),
        thin_options_volume=float(opt.get("thin_options_volume", 5000)),
        recent_windows=int(opt.get("recent_windows", 6)),
        options_strong=float(opt.get("strong_threshold", 60)),
        options_bias=float(opt.get("bias_threshold", 25)),
        confirm_cycles=max(1, int(al.get("confirm_cycles", 2))),
        cooldown_minutes=int(al.get("cooldown_minutes", 30)),
        divergence_min_options_score=float(al.get("divergence_min_options_score", 40)),
        iv_spike_points=float(al.get("iv_spike_points", 3.0)),
        iv_spike_window_minutes=int(al.get("iv_spike_window_minutes", 30)),
        block_cooldown_minutes=int(al.get("block_cooldown_minutes", 60)),
        telegram_enabled=bool(tg.get("enabled", True)),
        telegram_commands=bool(tg.get("commands", True)),
        web_enabled=bool(web.get("enabled", True)),
        screen_enabled=bool(scr.get("enabled", True)),
        screen_interval_minutes=max(30, int(scr.get("interval_minutes", 120))),
        web_host=web.get("host", "0.0.0.0"),
        web_port=int(os.environ.get("MIAS_WEB_PORT", web.get("port", 8080))),
        db_path=db_path,
        retention_days=int(st.get("retention_days", 30)),
        api_key=os.environ.get("MASSIVE_API_KEY", ""),
        telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
    )
    if require_secrets and not cfg.api_key:
        raise ConfigError("MASSIVE_API_KEY is not set")
    return cfg
