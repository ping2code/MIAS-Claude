"""Options chain analytics.

Everything is computed from the Massive chain snapshot (real-time quotes and trades on Options Advanced):

* implied spot  real-time underlying price from put-call parity (S ~ K + C - P) on the nearest expiry,
                for when the stock feed is delayed (Stocks Starter)
* flow          per-contract volume added since the previous cycle, signed by where the last trade
                printed against the quote (at/above mid = buyer, below = seller). This is an estimate:
                only the latest trade per contract is visible in a snapshot.
* positioning   put/call volume and open interest, ATM IV, 25-delta risk reversal, dealer gamma,
                call/put walls, unusual contracts
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from statistics import median
from zoneinfo import ZoneInfo

from .indicators import clamp, tanh

STALE_QUOTE_NS = 15 * 60 * 1_000_000_000


@dataclass(slots=True)
class Contract:
    ticker: str
    kind: str              # "call" | "put"
    strike: float
    expiry: date
    dte: int
    volume: float          # today's volume (0 if the day record is from an earlier session)
    day_vwap: float | None
    oi: float
    iv: float | None
    delta: float | None
    gamma: float | None
    bid: float | None
    ask: float | None
    quote_ns: int | None
    last_price: float | None
    trade_ns: int | None

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None or self.ask <= 0 or self.bid < 0 or self.ask < self.bid:
            return None
        return (self.bid + self.ask) / 2


@dataclass
class Window:
    """Flow added between two consecutive snapshots."""
    ts: float
    bull_premium: float = 0.0
    bear_premium: float = 0.0
    neutral_premium: float = 0.0
    net_delta: float = 0.0      # signed delta-dollars bought (+) / sold (-)
    gross_delta: float = 0.0
    call_volume: float = 0.0
    put_volume: float = 0.0

    def add(self, other: "Window") -> None:
        for k in ("bull_premium", "bear_premium", "neutral_premium", "net_delta", "gross_delta",
                  "call_volume", "put_volume"):
            setattr(self, k, getattr(self, k) + getattr(other, k))


@dataclass
class Print:
    ticker: str
    kind: str
    strike: float
    expiry: str
    side: str            # "buy" | "sell" | "mid"
    volume: float
    premium: float
    oi: float
    price: float | None


@dataclass
class Idea:
    """A liquid contract matching the swing template (expiry window, target |delta|). Not a recommendation."""
    kind: str
    expiry: str
    dte: int
    strike: float
    delta: float
    mid: float
    spread_pct: float
    oi: float
    volume: float
    iv: float | None       # percent
    # The short leg of a debit spread on the same expiry (about SPREAD_SHORT_DELTA), when a liquid one exists.
    short_strike: float | None = None
    short_mid: float | None = None
    short_delta: float | None = None

    @property
    def spread_cost(self) -> float | None:
        return round(self.mid - self.short_mid, 2) if self.short_mid is not None else None

    @property
    def spread_width(self) -> float | None:
        return abs(self.short_strike - self.strike) if self.short_strike is not None else None


SPREAD_SHORT_DELTA = 0.30


@dataclass
class OptionsSnapshot:
    contracts: int
    implied_spot: float | None
    call_volume: float
    put_volume: float
    call_premium: float
    put_premium: float
    call_oi: float
    put_oi: float
    pcr_volume: float | None
    pcr_oi: float | None
    atm_iv_front: float | None     # percent
    atm_iv_month: float | None     # percent
    rr25: float | None             # call IV - put IV at 25 delta, vol points
    gex: float                     # dealer gamma, $ per 1% move (call +, put -)
    call_wall: float | None
    put_wall: float | None
    unusual: list[Print] = field(default_factory=list)
    blocks: list[Print] = field(default_factory=list)
    window: Window | None = None
    quote_coverage: float = 0.0
    thin: bool = False                       # too little volume for flow to mean much
    ideas: dict[str, dict[str, Idea]] = field(default_factory=dict)   # template -> call/put -> Idea
    iv_term: list[tuple[str, int, float]] = field(default_factory=list)  # (expiry, dte, ATM IV %) up to 100 DTE

    def to_dict(self) -> dict:
        return asdict(self)


def parse_contract(item: dict, today: date, tz: ZoneInfo, session_day: date | None = None) -> Contract | None:
    """session_day: the trading day whose volume counts (defaults to today)."""
    try:
        d = item["details"]
        kind = d["contract_type"]
        strike = float(d["strike_price"])
        expiry = date.fromisoformat(d["expiration_date"])
        ticker = d["ticker"]
    except (KeyError, TypeError, ValueError):
        return None
    if kind not in ("call", "put") or expiry < today:
        return None
    day = item.get("day") or {}
    volume, vwap = 0.0, None
    day_ns = day.get("last_updated")
    if day_ns and _day(day_ns, tz) == (session_day or today):
        volume = float(day.get("volume") or 0)
        vwap = _f(day.get("vwap"))
    greeks = item.get("greeks") or {}
    q = item.get("last_quote") or {}
    t = item.get("last_trade") or {}
    return Contract(
        ticker=ticker, kind=kind, strike=strike, expiry=expiry, dte=(expiry - today).days,
        volume=volume, day_vwap=vwap, oi=float(item.get("open_interest") or 0),
        iv=_f(item.get("implied_volatility")), delta=_f(greeks.get("delta")), gamma=_f(greeks.get("gamma")),
        bid=_f(q.get("bid")), ask=_f(q.get("ask")), quote_ns=q.get("last_updated"),
        last_price=_f(t.get("price")), trade_ns=t.get("sip_timestamp"),
    )


def _day(ns: int, tz: ZoneInfo) -> date:
    return datetime.fromtimestamp(ns / 1e9, timezone.utc).astimezone(tz).date()


def latest_session(raw_chain: list[dict], today: date, tz: ZoneInfo) -> date:
    """The most recent trading day present in the chain's day records: today in session, the last session otherwise."""
    days = [_day(i["day"]["last_updated"], tz) for i in raw_chain
            if isinstance(i.get("day"), dict) and i["day"].get("last_updated")]
    days = [d for d in days if d <= today]
    return max(days) if days else today


def _f(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def implied_spot(contracts: list[Contract], ref_spot: float, now_ns: int) -> float | None:
    """Median of K + C_mid - P_mid over the strikes nearest the reference spot, on the nearest usable expiry."""
    by_exp: dict[date, dict[float, dict[str, Contract]]] = {}
    for c in contracts:
        if c.mid is None or c.quote_ns is None or now_ns - c.quote_ns > STALE_QUOTE_NS:
            continue
        spread = c.ask - c.bid
        if spread > max(0.10, 0.10 * c.mid):
            continue
        by_exp.setdefault(c.expiry, {}).setdefault(c.strike, {})[c.kind] = c
    for exp in sorted(by_exp):
        pairs = [(k, v) for k, v in by_exp[exp].items() if "call" in v and "put" in v]
        if len(pairs) < 3:
            continue
        pairs.sort(key=lambda kv: abs(kv[0] - ref_spot))
        estimates = [k + v["call"].mid - v["put"].mid for k, v in pairs[:4]]
        est = median(estimates)
        if ref_spot > 0 and abs(est - ref_spot) / ref_spot > 0.08:
            return None
        return round(est, 2)
    return None


def _side(c: Contract) -> int:
    """+1 buyer-initiated, -1 seller-initiated, 0 unknown/mid."""
    if c.last_price is None or c.mid is None:
        return 0
    spread = c.ask - c.bid
    if spread <= 0:
        return 0
    if c.last_price >= c.mid + 0.1 * spread:
        return 1
    if c.last_price <= c.mid - 0.1 * spread:
        return -1
    return 0


def _atm_iv(contracts: list[Contract], expiry: date, spot: float) -> float | None:
    strikes: dict[float, list[float]] = {}
    for c in contracts:
        if c.expiry == expiry and c.iv and c.iv > 0:
            strikes.setdefault(c.strike, []).append(c.iv)
    if not strikes:
        return None
    k = min(strikes, key=lambda s: abs(s - spot))
    return round(100 * sum(strikes[k]) / len(strikes[k]), 2)


def _rr25(contracts: list[Contract], expiry: date) -> float | None:
    calls = [c for c in contracts if c.expiry == expiry and c.kind == "call" and c.delta is not None and c.iv]
    puts = [c for c in contracts if c.expiry == expiry and c.kind == "put" and c.delta is not None and c.iv]
    if not calls or not puts:
        return None
    c = min(calls, key=lambda x: abs(x.delta - 0.25))
    p = min(puts, key=lambda x: abs(x.delta + 0.25))
    if abs(c.delta - 0.25) > 0.1 or abs(p.delta + 0.25) > 0.1:
        return None
    return round(100 * (c.iv - p.iv), 2)


def analyze(raw_chain: list[dict], *, ref_spot: float, prev_volumes: dict[str, float] | None, today: date,
            tz: ZoneInfo, now_ns: int, block_premium: float, unusual_min_premium: float,
            unusual_vol_oi_ratio: float, alert_0dte: bool = False, thin_volume: float = 0,
            idea_templates: dict | None = None) -> tuple[OptionsSnapshot, dict[str, float]]:
    """Analyze one chain snapshot. Returns the snapshot and today's volume per contract for the next cycle."""
    session_day = latest_session(raw_chain, today, tz)
    contracts = [c for c in (parse_contract(i, today, tz, session_day) for i in raw_chain) if c]
    spot_rt = implied_spot(contracts, ref_spot, now_ns)
    spot = spot_rt or ref_spot

    cv = pv = cp = pp = coi = poi = gex = 0.0
    call_oi_by_k: dict[float, float] = {}
    put_oi_by_k: dict[float, float] = {}
    unusual: list[Print] = []
    blocks: list[Print] = []
    window = Window(ts=now_ns / 1e9) if prev_volumes is not None else None
    volumes: dict[str, float] = {}
    quoted = 0

    for c in contracts:
        volumes[c.ticker] = c.volume
        if c.mid is not None:
            quoted += 1
        price = c.day_vwap or c.last_price or c.mid or 0.0
        prem = c.volume * price * 100
        if c.kind == "call":
            cv += c.volume; cp += prem; coi += c.oi
            call_oi_by_k[c.strike] = call_oi_by_k.get(c.strike, 0) + c.oi
        else:
            pv += c.volume; pp += prem; poi += c.oi
            put_oi_by_k[c.strike] = put_oi_by_k.get(c.strike, 0) + c.oi
        if c.gamma:
            gex += (1 if c.kind == "call" else -1) * c.gamma * c.oi * 100 * spot * spot * 0.01

        side = _side(c)
        side_name = {1: "buy", -1: "sell", 0: "mid"}[side]
        reportable = alert_0dte or c.dte > 0
        if reportable and c.volume >= max(1.0, c.oi * unusual_vol_oi_ratio) and prem >= unusual_min_premium:
            unusual.append(Print(c.ticker, c.kind, c.strike, c.expiry.isoformat(), side_name, c.volume, prem,
                                 c.oi, c.last_price))

        if window is None or c.ticker not in prev_volumes:
            continue
        dv = c.volume - prev_volumes[c.ticker]
        if dv <= 0:
            continue
        wprice = c.last_price or c.mid or c.day_vwap or 0.0
        wprem = dv * wprice * 100
        bullish = (side == 1) == (c.kind == "call")
        if side == 0:
            window.neutral_premium += wprem
        elif bullish:
            window.bull_premium += wprem
        else:
            window.bear_premium += wprem
        if c.delta is not None:
            d_dollars = dv * c.delta * 100 * spot
            window.net_delta += side * d_dollars
            window.gross_delta += abs(d_dollars)
        if c.kind == "call":
            window.call_volume += dv
        else:
            window.put_volume += dv
        if reportable and wprem >= block_premium:
            blocks.append(Print(c.ticker, c.kind, c.strike, c.expiry.isoformat(), side_name, dv, wprem, c.oi,
                                c.last_price))

    expiries = sorted({c.expiry for c in contracts})
    front = next((e for e in expiries if (e - today).days >= 1), expiries[0] if expiries else None)
    month = min(expiries, key=lambda e: abs((e - today).days - 30)) if expiries else None
    above = {k: v for k, v in call_oi_by_k.items() if k >= spot}
    below = {k: v for k, v in put_oi_by_k.items() if k <= spot}

    snap = OptionsSnapshot(
        contracts=len(contracts),
        implied_spot=spot_rt,
        call_volume=cv, put_volume=pv, call_premium=round(cp), put_premium=round(pp),
        call_oi=coi, put_oi=poi,
        pcr_volume=round(pv / cv, 3) if cv else None,
        pcr_oi=round(poi / coi, 3) if coi else None,
        atm_iv_front=_atm_iv(contracts, front, spot) if front else None,
        atm_iv_month=_atm_iv(contracts, month, spot) if month else None,
        rr25=_rr25(contracts, month) if month else None,
        gex=round(gex),
        call_wall=max(above, key=above.get) if above else None,
        put_wall=max(below, key=below.get) if below else None,
        unusual=sorted(unusual, key=lambda p: -p.premium)[:5],
        blocks=sorted(blocks, key=lambda p: -p.premium)[:5],
        window=window,
        quote_coverage=round(quoted / len(contracts), 3) if contracts else 0.0,
        thin=cv + pv < thin_volume,
        ideas={name: pick_contracts(contracts, t) for name, t in (idea_templates or {}).items()},
        iv_term=[(e.isoformat(), (e - today).days, iv) for e in expiries if 1 <= (e - today).days <= 100
                 for iv in [_atm_iv(contracts, e, spot)] if iv],
    )
    return snap, volumes


def pick_contracts(contracts: list[Contract], t) -> dict[str, Idea]:
    """Per side: closest to the template's |delta| (0.05 buckets), then near the middle of the expiry window
    (5-day buckets), then the most liquid (open interest + today's volume).
    `t` has min_dte, max_dte, delta, max_spread_pct, min_oi."""
    out: dict[str, Idea] = {}
    mid_dte = (t.min_dte + t.max_dte) / 2
    for kind in ("call", "put"):
        best, best_key = None, None
        for c in contracts:
            if c.kind != kind or not (t.min_dte <= c.dte <= t.max_dte) or c.delta is None or not c.mid:
                continue
            spread_pct = 100 * (c.ask - c.bid) / c.mid
            if spread_pct > t.max_spread_pct or c.oi < t.min_oi:
                continue
            key = (round(abs(abs(c.delta) - t.delta) / 0.05), round(abs(c.dte - mid_dte) / 5),
                   -(c.oi + c.volume), abs(abs(c.delta) - t.delta))
            if best_key is None or key < best_key:
                best, best_key = c, key
        if best and abs(abs(best.delta) - t.delta) <= 0.12:
            idea = Idea(kind, best.expiry.isoformat(), best.dte, best.strike, round(best.delta, 2),
                        round(best.mid, 2), round(100 * (best.ask - best.bid) / best.mid, 1), best.oi,
                        best.volume, round(100 * best.iv, 1) if best.iv else None)
            sign = 1 if kind == "call" else -1
            shorts = [c for c in contracts if c.kind == kind and c.expiry == best.expiry and c.delta is not None
                      and c.mid and sign * (c.strike - best.strike) > 0 and c.oi >= t.min_oi
                      and 100 * (c.ask - c.bid) / c.mid <= t.max_spread_pct]
            if shorts:
                sc = min(shorts, key=lambda c: abs(abs(c.delta) - SPREAD_SHORT_DELTA))
                if abs(abs(sc.delta) - SPREAD_SHORT_DELTA) <= 0.10:
                    idea.short_strike, idea.short_mid, idea.short_delta = sc.strike, round(sc.mid, 2), round(sc.delta, 2)
            out[kind] = idea
    return out


@dataclass
class OptionsBias:
    score: float
    label: str
    components: dict[str, float]
    session_net_premium: float
    recent_net_delta: float


def options_bias(snap: OptionsSnapshot, session: Window | None, recent: Window | None, rr_open: float | None,
                 *, strong: float, bias: float) -> OptionsBias:
    """Combine flow and positioning into a -100..+100 options bias."""
    from .trend import label_for

    comps: dict[str, float] = {}
    weights = {"session_flow": 0.35, "recent_flow": 0.30, "put_call": 0.15, "skew": 0.20}
    if session:
        gross = session.bull_premium + session.bear_premium + session.neutral_premium
        if gross > 0:
            comps["session_flow"] = clamp(2.5 * (session.bull_premium - session.bear_premium) / gross)
    if recent and recent.gross_delta > 0:
        comps["recent_flow"] = clamp(2.0 * recent.net_delta / recent.gross_delta)
    if snap.pcr_volume and snap.pcr_oi and snap.put_volume + snap.call_volume >= 1000:
        comps["put_call"] = tanh(math.log(snap.pcr_oi / snap.pcr_volume))
    if snap.rr25 is not None and rr_open is not None:
        comps["skew"] = tanh((snap.rr25 - rr_open) / 1.5)

    used = sum(weights[k] for k in comps)
    raw = sum(comps[k] * weights[k] for k in comps) / max(used, 0.6) if comps else 0.0
    score = round(100 * raw, 1)
    lbl = label_for(score, strong, bias).replace("_up", "_bullish").replace("_down", "_bearish")
    lbl = {"up": "bullish", "down": "bearish"}.get(lbl, lbl)
    net_prem = (session.bull_premium - session.bear_premium) if session else 0.0
    return OptionsBias(score, lbl, {k: round(v, 3) for k, v in comps.items()}, round(net_prem),
                       round(recent.net_delta) if recent else 0.0)
