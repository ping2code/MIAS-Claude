"""V3 config (early_watch_v3.toml). Detection values are a copy of current V2; presentation values are display only."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path

ONE = 60_000            # 1-minute bar span, ms
FIVE = 300_000          # 5-minute bar span, ms
FAMILIES = ("structure", "vwap", "or")
DEFAULT_PATH = Path("early_watch_v3.toml")


@dataclass(frozen=True)
class DetectRules:
    """The detection rules V3 runs (field names as in mias/observer/rules.py, plus the V2 family constants)."""
    pivot_k: int = 2
    dev_atr_mult: float = 0.25
    atr_period: int = 14
    ema_fast: int = 9
    ema_slow: int = 21
    session_open: time = time(9, 30)
    session_close: time = time(16, 0)
    or_end: time = time(9, 45)
    main_start: time = time(9, 45)
    main_end: time = time(15, 0)
    warm_sessions: int = 10
    stale_bars_after_gap: int = 2
    need_structure: int = 1
    need_vwap: int = 2
    need_or: int = 2
    vwap_prior_closes: int = 3
    vwap_invalid_closes: int = 2
    watch_minutes: int = 60

    @property
    def need(self) -> dict[str, int]:
        return {"structure": self.need_structure, "vwap": self.need_vwap, "or": self.need_or}


@dataclass(frozen=True)
class Presentation:
    marginal_atr: float = 0.10
    tight_invalidation_atr: float = 0.30
    low_conviction_rvol: float = 0.8
    volume_support_rvol: float = 1.0
    late_session_et: time = time(14, 30)


@dataclass(frozen=True)
class TelegramCfg:
    send: dict = field(default_factory=lambda: {f: True for f in FAMILIES})
    max_age_s: float = 180
    combine_s: int = 300
    group_gap_s: float = 3.0
    private_gap_s: float = 1.0
    max_wait_s: float = 60
    send_timeout_s: float = 180


@dataclass(frozen=True)
class V3Config:
    enabled: bool = True
    telegram_enabled: bool = True
    chat_id: str | None = None
    symbols: tuple[str, ...] = ()
    db_path: Path = Path("data/early_watch_v3/v3.db")
    cache_dir: Path = Path("data/early_watch_v3/cache")
    bars_db: Path = Path("data/observer/observer.db")
    source_cache_dir: Path | None = Path("data/observer/cache")
    v2_db: Path | None = Path("data/observer/v2.db")
    poll_s: float = 1.0
    confirm_poll_s: float = 20
    reload_s: float = 5
    day_start_from: time = time(9, 22)
    day_start_until: time = time(16, 30)
    end_of_day: time = time(16, 3)
    options_fresh_s: float = 330
    rules: DetectRules = DetectRules()
    tg: TelegramCfg = TelegramCfg()
    pres: Presentation = Presentation()
    path: Path | None = None


def _t(x, default: time) -> time:
    return time.fromisoformat(str(x)) if x is not None else default


def load(path: str | Path = DEFAULT_PATH) -> V3Config:
    path = Path(path)
    with open(path, "rb") as fh:
        raw = tomllib.load(fh).get("early_watch_v3", {})
    se, st, vw, orr = (raw.get(k, {}) for k in ("session", "structure", "vwap", "or"))
    wt, op, tg, pr = (raw.get(k, {}) for k in ("watch", "options", "telegram", "presentation"))
    d = DetectRules()
    rules = DetectRules(
        pivot_k=int(st.get("pivot_k", d.pivot_k)), dev_atr_mult=float(st.get("dev_atr_mult", d.dev_atr_mult)),
        atr_period=int(st.get("atr_period", d.atr_period)), ema_fast=int(st.get("ema_fast", d.ema_fast)),
        ema_slow=int(st.get("ema_slow", d.ema_slow)),
        session_open=_t(se.get("session_open"), d.session_open), session_close=_t(se.get("session_close"),
                                                                                   d.session_close),
        or_end=_t(se.get("or_end"), d.or_end), main_start=_t(se.get("main_start"), d.main_start),
        main_end=_t(se.get("main_end"), d.main_end),
        warm_sessions=int(st.get("warm_sessions", d.warm_sessions)),
        stale_bars_after_gap=int(st.get("stale_bars_after_gap", d.stale_bars_after_gap)),
        need_structure=int(st.get("persistence_closes", d.need_structure)),
        need_vwap=int(vw.get("persistence_closes", d.need_vwap)), need_or=int(orr.get("persistence_closes", d.need_or)),
        vwap_prior_closes=int(vw.get("prior_closes_other_side", d.vwap_prior_closes)),
        vwap_invalid_closes=int(vw.get("invalidation_closes_through", d.vwap_invalid_closes)),
        watch_minutes=int(wt.get("lifetime_minutes", d.watch_minutes)))
    t0 = TelegramCfg()
    tgc = TelegramCfg(send={f: bool(tg.get(f"send_{f}", True)) for f in FAMILIES},
                      max_age_s=float(tg.get("max_age_seconds", t0.max_age_s)),
                      combine_s=int(tg.get("combine_seconds", t0.combine_s)),
                      group_gap_s=float(tg.get("group_gap_seconds", t0.group_gap_s)),
                      private_gap_s=float(tg.get("private_gap_seconds", t0.private_gap_s)),
                      max_wait_s=float(tg.get("max_wait_seconds", t0.max_wait_s)),
                      send_timeout_s=float(tg.get("send_timeout_seconds", t0.send_timeout_s)))
    p0 = Presentation()
    pres = Presentation(marginal_atr=float(pr.get("marginal_atr", p0.marginal_atr)),
                        tight_invalidation_atr=float(pr.get("tight_invalidation_atr", p0.tight_invalidation_atr)),
                        low_conviction_rvol=float(pr.get("low_conviction_rvol", p0.low_conviction_rvol)),
                        volume_support_rvol=float(pr.get("volume_support_rvol", p0.volume_support_rvol)),
                        late_session_et=_t(pr.get("late_session_et"), p0.late_session_et))
    syms = [str(s).upper() for s in raw.get("symbols") or []]
    syms = ["SPY"] + [s for s in syms if s != "SPY"]           # = V2: SPY first (market context)
    chat = raw.get("telegram_chat_id_v3")
    src_cache = raw.get("source_cache_dir")
    v2db = raw.get("v2_db")
    return V3Config(
        enabled=bool(raw.get("enabled", False)), telegram_enabled=bool(raw.get("telegram_enabled", False)),
        chat_id=str(chat) if chat not in (None, "") else None, symbols=tuple(syms),
        db_path=Path(raw.get("db_path", "data/early_watch_v3/v3.db")),
        cache_dir=Path(raw.get("cache_dir", "data/early_watch_v3/cache")),
        bars_db=Path(raw.get("bars_db", "data/observer/observer.db")),
        source_cache_dir=Path(src_cache) if src_cache else None, v2_db=Path(v2db) if v2db else None,
        poll_s=float(raw.get("poll_seconds", 1.0)), confirm_poll_s=float(raw.get("confirm_poll_seconds", 20)),
        reload_s=float(raw.get("reload_seconds", 5)),
        day_start_from=_t(se.get("day_start_from"), time(9, 22)),
        day_start_until=_t(se.get("day_start_until"), time(16, 30)), end_of_day=_t(se.get("end_of_day"), time(16, 3)),
        options_fresh_s=float(op.get("fresh_seconds", 330)), rules=rules, tg=tgc, pres=pres, path=path)


def hot(path: str | Path) -> dict:
    """The settings V3 re-reads while running (kill switch + family send flags)."""
    c = load(path)
    return {"enabled": c.enabled, "telegram_enabled": c.telegram_enabled, "send": dict(c.tg.send)}
