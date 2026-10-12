"""Options-tradability screen: which underlyings are best to trade options on, and why.

Score (0-100) = 35% liquidity + 35% spread tightness + 15% movement + 15% fair pricing, each a percentile rank
within the screened universe. Hard requirements: weekly expirations, enough premium traded, and a usable
median spread. See `python -m mias screen --help`.
"""
from __future__ import annotations

import asyncio
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from statistics import median

from .massive import MassiveClient
from .options import latest_session, parse_contract
from .trend import realized_vol, to_bars

SECTORS = {
    "Index/ETF": "SPY QQQ IWM DIA SMH SOXL TQQQ GLD SLV TLT XLE XLF ARKK",
    "Technology": "NVDA AAPL MSFT AVGO ORCL AMD MU INTC CSCO CRM ADBE QCOM TXN IBM NOW PLTR AMAT LRCX KLAC MRVL "
                  "ARM SMCI DELL ANET PANW CRWD SNOW SNDK TSM FICO",
    "Communication": "META GOOGL NFLX DIS T VZ TMUS CMCSA WBD SPOT RDDT PINS SNAP CHTR ROKU EA TTWO",
    "Consumer Disc.": "AMZN TSLA HD MCD NKE SBUX LOW BKNG TJX CMG ABNB UBER RIVN LCID GM F DKNG LULU CVNA",
    "Consumer Staples": "WMT COST PG KO PEP PM MO MDLZ CL TGT KR EL KHC GIS STZ DG",
    "Financials": "JPM BAC WFC C GS MS V MA AXP SCHW BLK HOOD COIN PYPL SOFI AFRM UPST MSTR",
    "Health Care": "LLY UNH JNJ ABBV MRK PFE AMGN GILD TMO ABT ISRG BMY CVS NVO HIMS MRNA VRTX REGN",
    "Energy": "XOM CVX COP OXY SLB EOG HAL DVN MPC PSX VLO KMI WMB OKE FANG APA",
    "Industrials": "BA CAT GE HON UPS FDX DE LMT RTX UNP ETN GEV DAL UAL AAL LUV",
    "Materials": "LIN FCX NEM SHW NUE DOW DD APD CLF MP ALB AA ECL",
    "Real Estate": "PLD AMT EQIX O SPG CCI PSA WELL DLR VICI OPEN CBRE EXR AVB",
    "Utilities": "NEE DUK SO D AEP EXC SRE PCG CEG VST XEL ED NRG ETR",
}

MIN_PREMIUM = 10e6          # $ traded today
MAX_SPREAD_PCT = 5.0        # median spread on tradable contracts
MIN_WEEKLY_EXPIRIES = 3     # expiries within 30 days


@dataclass
class Row:
    symbol: str
    sector: str
    price: float
    change_pct: float | None
    premium: float
    volume: float
    oi: float
    spread_day: float | None      # median % spread, |delta| 0.35-0.65, 1-14 DTE
    spread_swing: float | None    # same, 50-100 DTE
    expiries_30d: int
    rv20: float | None
    iv30: float | None
    swing_cost: float | None      # $ per contract, ~0.6 delta, 50-100 DTE call
    score: float = 0.0
    passes: bool = False
    why_not: str = ""

    @property
    def iv_rv(self) -> float | None:
        return self.iv30 / self.rv20 if self.iv30 and self.rv20 else None


def universe(extra: list[str] = ()) -> dict[str, str]:
    out = {s: sector for sector, syms in SECTORS.items() for s in syms.split()}
    for s in extra:
        out.setdefault(s, "Other")
    return out


async def collect(client: MassiveClient, symbols: dict[str, str], tz, today: date) -> list[Row]:
    snaps = await client.snapshots(list(symbols))

    async def one(sym: str) -> Row | None:
        snap = snaps.get(sym) or {}
        sess = snap.get("session") or {}
        price = sess.get("price")
        if not price or snap.get("error"):
            return None
        try:
            chain, _ = await client.option_chain(sym, spot=price, today=today, max_dte=100, strike_pct=0.20,
                                                 max_pages=60)
            daily = await client.aggregates(sym, 1, "day", today - timedelta(days=45), today - timedelta(days=1))
        except Exception:
            return None
        session_day = latest_session(chain, today, tz)
        cs = [c for c in (parse_contract(i, today, tz, session_day) for i in chain) if c]
        if not cs:
            return None
        premium = sum(c.volume * (c.day_vwap or c.last_price or 0) * 100 for c in cs)

        def spreads(lo: int, hi: int) -> float | None:
            # Weighted by activity (today's volume + OI/10): thinly traded Monday/Wednesday expiries and
            # half-dollar strikes must not hide how tight the contracts people actually trade are.
            xs = [(100 * (c.ask - c.bid) / c.mid, c.volume + c.oi / 10) for c in cs
                  if lo <= c.dte <= hi and c.delta is not None and 0.35 <= abs(c.delta) <= 0.65 and c.mid]
            return weighted_median(xs)

        swing = [c for c in cs if c.kind == "call" and 50 <= c.dte <= 100 and c.delta and c.mid]
        swing_cost = None
        if swing:
            best = min(swing, key=lambda c: abs(c.delta - 0.6))
            swing_cost = round(best.mid * 100)
        month = [c for c in cs if c.iv and c.delta is not None and 0.4 <= abs(c.delta) <= 0.6]
        iv30 = None
        if month:
            target = min({c.dte for c in month}, key=lambda d: abs(d - 30))
            ivs = [c.iv for c in month if c.dte == target]
            iv30 = round(100 * median(ivs), 1)
        return Row(sym, symbols[sym], price, sess.get("change_percent"), premium,
                   sum(c.volume for c in cs), sum(c.oi for c in cs), spreads(1, 14), spreads(50, 100),
                   len({c.expiry for c in cs if c.dte <= 30}), realized_vol(to_bars(daily)), iv30, swing_cost)

    rows = await asyncio.gather(*(one(s) for s in symbols))
    return [r for r in rows if r]


def weighted_median(pairs: list[tuple[float, float]]) -> float | None:
    pairs = sorted((v, w) for v, w in pairs if w > 0)
    total = sum(w for _, w in pairs)
    if not total:
        return None
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= total / 2:
            return round(v, 2)
    return round(pairs[-1][0], 2)


def _pct_rank(values: list[float], higher_is_better: bool = True) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i], reverse=not higher_is_better)
    ranks = [0.0] * len(values)
    for pos, i in enumerate(order):
        ranks[i] = 100 * pos / max(1, len(values) - 1)
    return ranks


def score(rows: list[Row]) -> list[Row]:
    if not rows:
        return rows
    liq = _pct_rank([0.5 * math.log1p(r.premium) + 0.5 * math.log1p(r.oi) for r in rows])
    spr = [statistics_or(r.spread_day, r.spread_swing) for r in rows]
    tight = _pct_rank([s if s is not None else 99.0 for s in spr], higher_is_better=False)
    move = _pct_rank([r.rv20 or 0.0 for r in rows])
    # Fair pricing: IV/RV closest to ~1.0 is best; very rich or missing scores low.
    fair = _pct_rank([-abs((r.iv_rv or 3.0) - 1.0) for r in rows])
    for r, l, t, m, f, s in zip(rows, liq, tight, move, fair, spr):
        r.score = round(0.35 * l + 0.35 * t + 0.15 * m + 0.15 * f, 1)
        reasons = []
        if r.premium < MIN_PREMIUM:
            reasons.append(f"only ${r.premium / 1e6:.1f}M premium")
        if s is None or s > MAX_SPREAD_PCT:
            reasons.append("wide spreads")
        if r.expiries_30d < MIN_WEEKLY_EXPIRIES:
            reasons.append("no weeklies")
        r.passes, r.why_not = not reasons, ", ".join(reasons)
    return sorted(rows, key=lambda r: (-r.passes, -r.score))


def statistics_or(a: float | None, b: float | None) -> float | None:
    xs = [x for x in (a, b) if x is not None]
    return round(sum(xs) / len(xs), 2) if xs else None


def report(rows: list[Row], top: int, when: datetime) -> str:
    good = [r for r in rows if r.passes]
    lines = [f"Options tradability screen · {when:%Y-%m-%d %H:%M} ET · {len(rows)} screened, {len(good)} pass",
             "Score = 35% liquidity + 35% tight spreads + 15% movement + 15% fair IV (percentiles)\n",
             f"{'#':>3} {'SYM':<6}{'SECTOR':<17}{'SCORE':>6}{'PREM $M':>9}{'OI K':>8}{'SPRD D/S %':>12}"
             f"{'RV20':>6}{'IV/RV':>6}{'SWING $':>9}"]
    for i, r in enumerate(good[:top], 1):
        sd = f"{r.spread_day:.1f}" if r.spread_day is not None else "–"
        ss = f"{r.spread_swing:.1f}" if r.spread_swing is not None else "–"
        lines.append(f"{i:>3} {r.symbol:<6}{r.sector:<17}{r.score:>6.0f}{r.premium / 1e6:>9,.0f}{r.oi / 1e3:>8,.0f}"
                     f"{sd + '/' + ss:>12}{(r.rv20 or 0):>6.0f}{(r.iv_rv or 0):>6.2f}"
                     f"{(r.swing_cost or 0):>9,.0f}")
    lines.append("\nExcluded (sample): " + "; ".join(f"{r.symbol} ({r.why_not})" for r in rows if not r.passes)[:900])
    return "\n".join(lines)


MIN_ROWS_TO_SAVE = 20       # fewer usable rows means a data problem: keep the previous result


async def run(client: MassiveClient, symbols: list[str], tz, now: datetime) -> dict | None:
    """Full screen as a JSON-serializable dict, or None when too little data came back."""
    rows = score(await collect(client, universe(symbols), tz, now.date()))
    if len(rows) < MIN_ROWS_TO_SAVE:
        return None
    return {"at": now.isoformat(), "screened": len(rows),
            "rows": [{**asdict(r), "iv_rv": round(r.iv_rv, 2) if r.iv_rv else None} for r in rows]}
