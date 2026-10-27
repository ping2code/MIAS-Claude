"""Configuration: config.toml for behaviour, environment for secrets."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import date
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
class Instrument:
    """A symbol whose data comes from elsewhere, e.g. an index without real-time index data."""
    options_symbol: str            # chain to analyze, e.g. "I:NDX"
    trend_symbol: str              # bars used for the trend, e.g. "QQQ"
    close_symbol: str | None       # daily bars for the previous close, e.g. "I:NDX"
    chain_strike_pct: float | None # narrower strike window for very large chains
    note: str = ""


@dataclass(frozen=True)
class PositionRules:
    take_profit: float = 0.55        # spreads: share of max profit
    single_take_profit: float = 0.50 # single legs: gain on the premium paid
    stop_loss: float = 0.50          # loss on the premium paid
    near_strike_pct: float = 1.5     # underlying within this % of the short strike
    time_stop_days: int = 10         # trading days after entry, unless the position has its own exit date
    hard_exit_days: int = 7          # calendar days before expiry


@dataclass(frozen=True)
class Sector:
    key: str                       # config key and /sector argument, e.g. "energy"
    name: str                      # display name, e.g. "Energy"
    etf: str | None                # sector gauge, e.g. "XLE"
    symbols: list[str]


ALERT_MODES = ("tiered", "strong_alignment", "all")


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
    alert_mode: str
    alignment_end_alert: bool
    change_enter: float
    change_exit: float
    strong_exit: float
    options_enter: float
    options_exit: float
    changes_every_minutes: int
    tier1_merge_minutes: int
    pullback_dip: float
    pullback_recover: float
    pullback_cooldown_minutes: int
    report_block_premium: float
    positions: "PositionRules"
    instruments: dict[str, Instrument]
    sectors: list[Sector]
    reports: dict[str, int]
    report_until: dict[str, date]
    unusual_every_minutes: int
    unusual_top: int
    picks_enabled: bool
    picks_time: str
    confirm_cycles: int
    cooldown_minutes: int
    divergence_min_options_score: float
    iv_spike_points: float
    iv_spike_window_minutes: int
    block_cooldown_minutes: int
    iv_spike_series: str                 # "month" (~30-day ATM IV) or "front" (nearest expiry, noisy)
    options_confirm_cycles: int          # tiered mode: cycles an options flip must hold
    daytrade: "DayTradeRules"            # [daytrade]: the 5-minute EMA cross-and-retest paper trades
    brief_enabled: bool                  # [brief]: the morning day-trade brief
    brief_time: str
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
    telegram_alert_chats: list[str] = field(repr=False, default_factory=list)
    telegram_allowed_users: list[str] = field(repr=False, default_factory=list)
    telegram_allowed_chats: list[str] = field(repr=False, default_factory=list)


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


def _ids(name: str) -> list[str]:
    """Comma-separated Telegram user/chat IDs from the environment (kept out of the public config)."""
    return [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]


def _daytrade(raw: dict):
    from .emacross import DayTradeRules
    kw = {}
    for k, v in raw.items():
        if not hasattr(DayTradeRules, k):
            raise ConfigError(f"daytrade.{k} is not a setting")
        if k == "symbols":
            v = tuple(x.upper() for x in v)
        elif k == "trail":
            v = tuple((float(a), float(b)) for a, b in v)
        else:
            v = type(getattr(DayTradeRules, k))(v)
        kw[k] = v
    return DayTradeRules(**kw)


def load(path: str | Path = "config.toml", *, require_secrets: bool = True) -> Config:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    load_dotenv(path.parent / ".env")
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    def sec(name: str) -> dict:
        return raw.get(name, {})

    wl, sch, data, tr, opt, al, tg, web, st, scr, ins, rep = (sec(n) for n in (
        "watchlist", "schedule", "data", "trend", "options", "alerts", "telegram", "web", "storage", "screen",
        "instruments", "reports"))

    symbols = [s.strip().upper() for s in os.environ.get("MIAS_SYMBOLS", "").split(",") if s.strip()] \
        or [s.upper() for s in wl.get("symbols", [])]
    if not symbols:
        raise ConfigError("watchlist.symbols is empty")
    if len(set(symbols)) != len(symbols):
        raise ConfigError("watchlist.symbols contains duplicates")
    sectors = []
    for key, sct in raw.get("sectors", {}).items():
        members = [x.upper() for x in sct.get("symbols", [])]
        if not members:
            raise ConfigError(f"sectors.{key}.symbols is empty")
        etf = sct.get("etf", "").upper() or None
        sectors.append(Sector(key.lower(), sct.get("name", key), etf, members))
        if not os.environ.get("MIAS_SYMBOLS"):
            symbols += [x for x in [*members, *([etf] if etf else [])] if x not in symbols]

    profiles = {name: {k: float(v) for k, v in w.items()}
                for name, w in tr.get("profiles", DEFAULT_PROFILES).items()}
    for name, w in profiles.items():
        unknown = set(w) - TIMEFRAMES
        if unknown or not w:
            raise ConfigError(f"trend.profiles.{name} must use {sorted(TIMEFRAMES)}, got {sorted(w)}")
    primary = tr.get("profile", "swing")
    if primary not in profiles:
        raise ConfigError(f"trend.profile {primary!r} is not one of {sorted(profiles)}")

    mode = al.get("mode", "tiered")
    if mode not in ALERT_MODES:
        raise ConfigError(f"alerts.mode must be one of {ALERT_MODES}, got {mode!r}")
    instruments = {}
    for name, i in ins.items():
        if "options_symbol" not in i or "trend_symbol" not in i:
            raise ConfigError(f"instruments.{name} needs options_symbol and trend_symbol")
        pct = i.get("chain_strike_pct")
        instruments[name.upper()] = Instrument(i["options_symbol"], i["trend_symbol"].upper(), i.get("close_symbol"),
                                               float(pct) if pct is not None else None, i.get("note", ""))
    reports = {k.upper(): int(v) for k, v in rep.get("every_minutes", {}).items()}
    if any(m < 5 for m in reports.values()):
        raise ConfigError("reports.every_minutes values must be >= 5")
    if 0 < int(rep.get("unusual_every_minutes", 0)) < 5:
        raise ConfigError("reports.unusual_every_minutes must be 0 (off) or >= 5")
    try:
        report_until = {k.upper(): date.fromisoformat(str(v)) for k, v in rep.get("until", {}).items()}
    except ValueError as exc:
        raise ConfigError(f"reports.until must hold YYYY-MM-DD dates: {exc}") from None

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
        alert_mode=mode,
        alignment_end_alert=bool(al.get("alignment_end_alert", True)),
        change_enter=float(al.get("change_enter", 30)),
        change_exit=float(al.get("change_exit", 15)),
        strong_exit=float(al.get("strong_exit", 50)),
        options_enter=float(al.get("options_enter", 40)),
        options_exit=float(al.get("options_exit", 20)),
        changes_every_minutes=max(5, int(al.get("changes_every_minutes", 15))),
        tier1_merge_minutes=int(al.get("tier1_merge_minutes", 30)),
        pullback_dip=float(al.get("pullback_dip", 15)),
        pullback_recover=float(al.get("pullback_recover", 25)),
        pullback_cooldown_minutes=int(al.get("pullback_cooldown_minutes", 120)),
        report_block_premium=float(al.get("report_block_premium", 5_000_000)),
        positions=PositionRules(**{k: type(getattr(PositionRules, k))(v)
                                   for k, v in raw.get("positions", {}).items()}),
        instruments=instruments,
        sectors=sectors,
        reports=reports,
        report_until=report_until,
        unusual_every_minutes=int(rep.get("unusual_every_minutes", 0)),
        unusual_top=int(rep.get("unusual_top", 10)),
        picks_enabled=bool(raw.get("picks", {}).get("enabled", True)),
        picks_time=raw.get("picks", {}).get("time", "10:00"),
        confirm_cycles=max(1, int(al.get("confirm_cycles", 2))),
        cooldown_minutes=int(al.get("cooldown_minutes", 30)),
        divergence_min_options_score=float(al.get("divergence_min_options_score", 40)),
        iv_spike_points=float(al.get("iv_spike_points", 3.0)),
        iv_spike_window_minutes=int(al.get("iv_spike_window_minutes", 30)),
        block_cooldown_minutes=int(al.get("block_cooldown_minutes", 60)),
        iv_spike_series=al.get("iv_spike_series", "month"),
        options_confirm_cycles=max(1, int(al.get("options_confirm_cycles", al.get("confirm_cycles", 2)))),
        daytrade=_daytrade(raw.get("daytrade", {})),
        brief_enabled=bool(raw.get("brief", {}).get("enabled", True)),
        brief_time=raw.get("brief", {}).get("time", "09:45"),
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
        telegram_alert_chats=_ids("TELEGRAM_ALERT_CHATS") or _ids("TELEGRAM_CHAT_ID"),
        telegram_allowed_users=_ids("TELEGRAM_ALLOWED_USERS") or _ids("TELEGRAM_CHAT_ID"),
        telegram_allowed_chats=_ids("TELEGRAM_ALLOWED_CHATS") or _ids("TELEGRAM_ALERT_CHATS") or _ids("TELEGRAM_CHAT_ID"),
    )
    if require_secrets and not cfg.api_key:
        raise ConfigError("MASSIVE_API_KEY is not set")
    return cfg
