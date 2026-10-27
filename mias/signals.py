"""Alert rules. Edge-triggered with confirmation and cooldowns, state persisted across restarts.

Mode "tiered" (default) splits alerts by urgency, see `evaluate_tiered`:
  tier 1  sent at once:     pullback_entry, strong_alignment
  tier 2  batched (15 min): trend and options changes with hysteresis, wall breaks, IV spikes, alignment ended
  tier 3  blocks are folded into the unusual-activity report instead of being sent one by one
Mode "strong_alignment" alerts only on full agreement, see `evaluate_strong`.
Mode "all" uses `evaluate`:

Per symbol, one message per cycle combines every reason that fired:
  trend_change    composite stock trend class changed (bull / neutral / bear), confirmed N cycles
  options_flip    options bias class changed, confirmed N cycles
  aligned         stock and options agree on a direction (newly)
  divergence      stock and options point opposite ways with a meaningful options bias (newly)
  block           a single contract traded >= block_premium since the previous cycle
  iv_spike        ATM IV (30-day by default, see iv_spike_series) rose >= N vol points within the window
  wall_break      live price crossed the previous cycle's call wall (up) or put wall (down)
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .models import SymbolResult
from .trend import direction

DIR_WORD = {"bull": "BULLISH", "bear": "BEARISH", "neutral": "NEUTRAL"}


@dataclass
class Reason:
    rule: str
    direction: str
    text: str


def _confirm(slot: dict, new: str, need: int) -> tuple[str, str] | None:
    """Track a confirmed class. Returns (old, new) once a different class has held `need` cycles."""
    cur = slot.get("cls")
    if cur is None:
        slot.update(cls=new, pending=None, count=0)
        return None
    if new == cur:
        slot.update(pending=None, count=0)
        return None
    if slot.get("pending") == new:
        slot["count"] = slot.get("count", 0) + 1
    else:
        slot.update(pending=new, count=1)
    if slot["count"] >= need:
        slot.update(cls=new, pending=None, count=0)
        return cur, new
    return None


def _cooled(state: dict, key: str, now: float, minutes: float) -> bool:
    last = state.setdefault("last", {}).get(key)
    if last is not None and now - last < minutes * 60:
        return False
    state["last"][key] = now
    return True


def evaluate(r: SymbolResult, state: dict, cfg: Config, now: float,
             iv_history: list[tuple[float, float]]) -> list[Reason]:
    """Update `state` in place and return the reasons to alert on for this symbol."""
    reasons: list[Reason] = []
    stock_change = opt_change = None

    if r.stock and r.stock.timeframes:
        stock_change = _confirm(state.setdefault("stock", {}), direction(r.stock.label), cfg.confirm_cycles)
        if stock_change and _cooled(state, "trend_change", now, cfg.cooldown_minutes):
            old, new = stock_change
            reasons.append(Reason("trend_change", new,
                                  f"Stock trend {DIR_WORD[old]} → {DIR_WORD[new]} ({r.stock.score:+.0f})"))

    if r.bias and r.bias.components and not (r.options and r.options.thin):
        opt_change = _confirm(state.setdefault("options", {}), direction(r.bias.label), cfg.confirm_cycles)
        if opt_change and _cooled(state, "options_flip", now, cfg.cooldown_minutes):
            old, new = opt_change
            reasons.append(Reason("options_flip", new,
                                  f"Options bias {DIR_WORD[old]} → {DIR_WORD[new]} ({r.bias.score:+.0f})"))

    s_cls = state.get("stock", {}).get("cls")
    o_cls = state.get("options", {}).get("cls")
    if s_cls and o_cls:
        relation = "none"
        if s_cls == o_cls and s_cls != "neutral":
            relation = f"aligned_{s_cls}"
        elif {s_cls, o_cls} == {"bull", "bear"} and r.bias and direction(r.bias.label) == o_cls and \
                abs(r.bias.score) >= cfg.divergence_min_options_score:
            relation = f"diverge_{o_cls}"
        prev = state.get("relation")
        state["relation"] = relation
        if prev is not None and relation != prev and relation != "none":
            if relation.startswith("aligned") and _cooled(state, "aligned", now, cfg.cooldown_minutes):
                reasons.append(Reason("aligned", s_cls,
                                      f"Stock and options ALIGNED {DIR_WORD[s_cls]}"))
            elif relation.startswith("diverge") and _cooled(state, "divergence", now, cfg.cooldown_minutes):
                reasons.append(Reason("divergence", o_cls,
                                      f"DIVERGENCE: stock {DIR_WORD[s_cls]} but options {DIR_WORD[o_cls]}"))

    if r.options:
        for p in r.options.blocks:
            if p.side == "mid":
                continue
            if _cooled(state, f"block:{p.ticker}", now, cfg.block_cooldown_minutes):
                bullish = (p.side == "buy") == (p.kind == "call")
                reasons.append(Reason("block", "bull" if bullish else "bear",
                                      f"Block: {p.side.upper()} {p.volume:,.0f}x {p.expiry} {p.strike:g}"
                                      f"{p.kind[0].upper()} @ {p.price or 0:.2f} = ${p.premium / 1e6:.1f}M"))

        reasons += _iv_and_walls(r, state, cfg, now, iv_history)

    # Direction changes lead the message.
    order = {"trend_change": 0, "options_flip": 1, "aligned": 2, "divergence": 2, "wall_break": 3,
             "block": 4, "iv_spike": 5}
    reasons.sort(key=lambda x: order.get(x.rule, 9))
    return reasons


def spike_iv(options, series: str) -> float | None:
    """The ATM IV watched for spikes, from an OptionsSnapshot or its stored dict."""
    get = options.get if isinstance(options, dict) else lambda k: getattr(options, k, None)
    month, front = get("atm_iv_month"), get("atm_iv_front")
    return (month if month is not None else front) if series == "month" else front


def _iv_and_walls(r: SymbolResult, state: dict, cfg: Config, now: float,
                  iv_history: list[tuple[float, float]]) -> list[Reason]:
    """IV spike and call/put wall breaks (needs r.options)."""
    reasons = []
    iv = spike_iv(r.options, cfg.iv_spike_series)
    if iv is not None and iv_history:
        low = min(v for _, v in iv_history)
        if iv - low >= cfg.iv_spike_points and _cooled(state, "iv_spike", now, cfg.cooldown_minutes):
            d = "bear" if (r.day_change_pct or 0) < 0 else "neutral"
            reasons.append(Reason("iv_spike", d, f"IV spike: {cfg.iv_spike_series} ATM IV {low:.1f}% → {iv:.1f}% "
                                                 f"in {cfg.iv_spike_window_minutes}m"))

    price = r.price
    walls = state.get("walls") or {}
    prev_price = state.get("price")
    if price and prev_price and r.price_is_live:
        cw, pw = walls.get("call"), walls.get("put")
        if cw and prev_price < cw <= price and _cooled(state, "wall_up", now, cfg.cooldown_minutes):
            reasons.append(Reason("wall_break", "bull", f"Broke ABOVE call wall {cw:g} (now {price:.2f})"))
        if pw and prev_price > pw >= price and _cooled(state, "wall_down", now, cfg.cooldown_minutes):
            reasons.append(Reason("wall_break", "bear", f"Broke BELOW put wall {pw:g} (now {price:.2f})"))
    if price:
        state["price"] = price
    state["walls"] = {"call": r.options.call_wall, "put": r.options.put_wall}
    return reasons


def market_composite(results: list[SymbolResult], symbols: list[str]) -> tuple[float, float | None] | None:
    rs = [r for r in results if r.symbol in symbols and r.stock]
    if not rs:
        return None
    stock = round(sum(r.stock.score for r in rs) / len(rs), 1)
    ob = [r.bias.score for r in rs if r.bias and r.bias.components]
    return stock, (round(sum(ob) / len(ob), 1) if ob else None)


def strong_side(r: SymbolResult) -> str:
    """bull: swing STRONG UP + intraday STRONG UP + options bullish; bear: the mirror; none otherwise."""
    if not r.stock or not r.bias or not r.bias.components or (r.options and r.options.thin):
        return "none"
    swing = r.stock.composites.get("swing", (r.stock.score, r.stock.label))[1]
    intra = r.stock.composites.get("intraday", (None, ""))[1]
    opt = direction(r.bias.label)
    if swing == "strong_up" and intra == "strong_up" and opt == "bull":
        return "bull"
    if swing == "strong_down" and intra == "strong_down" and opt == "bear":
        return "bear"
    return "none"


def evaluate_strong(r: SymbolResult, state: dict, cfg: Config, now: float) -> list[Reason]:
    """Alert when a symbol enters full strong alignment (confirmed), and optionally when it leaves it."""
    # Start from "none" so an alignment already in place when this mode is switched on still gets alerted.
    change = _confirm(state.setdefault("strong", {"cls": "none", "pending": None, "count": 0}),
                      strong_side(r), cfg.confirm_cycles)
    if not change:
        return []
    old, new = change
    if new != "none":
        if not _cooled(state, f"strong_{new}", now, cfg.cooldown_minutes):
            return []
        word = "BULLISH" if new == "bull" else "BEARISH"
        tf = "UP" if new == "bull" else "DOWN"
        return [Reason("strong_alignment", new,
                       f"STRONG {word} ALIGNMENT: swing STRONG {tf} · intraday STRONG {tf} · options {word}")]
    if cfg.alignment_end_alert:
        word = "BULLISH" if old == "bull" else "BEARISH"
        return [Reason("alignment_ended", "neutral", f"Strong {word.lower()} alignment ENDED")]
    return []


# ---- tiered mode -----------------------------------------------------------------------------

LEVEL = {"strong_down": -2, "down": -1, "neutral": 0, "up": 1, "strong_up": 2}
WORD = {"strong_down": "STRONG DOWN", "down": "DOWN", "neutral": "NEUTRAL", "up": "UP", "strong_up": "STRONG UP"}


def hysteresis(cur: str | None, score: float, enter: tuple[float, float], exit_: tuple[float, float]) -> str:
    """Trend class with separate entry and exit thresholds, as (strong, bias) pairs, e.g. enter (60, 30) and
    exit (50, 15): a class is entered past the entry threshold and only left once the score falls back past
    the exit threshold, so a score wobbling around one line doesn't flip the class every cycle."""
    from .trend import label_for
    cand = label_for(score, *enter)
    if cur is None:
        return cand
    c, n = LEVEL[cur], LEVEL[cand]
    if n * c < 0 or abs(n) > abs(c):
        return cand                               # outward, or straight through to the other side
    loose = label_for(score, *exit_)
    return loose if abs(LEVEL[loose]) < abs(c) else cur


def _options_class(score: float, enter: float, exit_: float, cur: str | None) -> str:
    """bull / neutral / bear for the options bias, with hysteresis."""
    if score >= enter:
        return "bull"
    if score <= -enter:
        return "bear"
    if cur == "bull" and score > exit_:
        return "bull"
    if cur == "bear" and score < -exit_:
        return "bear"
    return "neutral"


def _confirmed_class(state: dict, slot: str, new: str, need: int) -> tuple[str, str] | None:
    """Like _confirm, but `new` is computed against the confirmed class (for hysteresis)."""
    return _confirm(state.setdefault(slot, {}), new, need)


def pullback(r: SymbolResult, state: dict, cfg: Config) -> str | None:
    """'bull' when an uptrend's intraday dip has turned back up (or 'bear' for the mirror), confirmed.

    Swing must be UP or STRONG UP; intraday must first fall to -pullback_dip or below, then recover to
    +pullback_recover or above for confirm_cycles; options must not be against it."""
    if not r.stock or "intraday" not in r.stock.composites:
        return None
    swing = r.stock.score
    intra = r.stock.composites["intraday"][0]
    opt = r.bias.score if r.bias and r.bias.components and not (r.options and r.options.thin) else 0.0
    slot = state.setdefault("pullback", {})
    fired = None
    for side, sign in (("bull", 1), ("bear", -1)):
        mark = slot.get(side) or {}
        if sign * swing < cfg.trend_bias:
            slot[side] = None                     # the swing trend is gone: forget any dip
            continue
        if sign * intra <= -cfg.pullback_dip:
            slot[side] = {"dip": min(sign * intra, mark.get("dip", 0)), "count": 0}
        elif mark and sign * intra >= cfg.pullback_recover and sign * opt > -cfg.options_bias:
            mark["count"] = mark.get("count", 0) + 1
            if mark["count"] >= cfg.confirm_cycles:
                slot[side] = None
                fired = side
            else:
                slot[side] = mark
        elif mark:
            mark["count"] = 0
            slot[side] = mark
    return fired


def evaluate_tiered(r: SymbolResult, state: dict, cfg: Config, now: float,
                    iv_history: list[tuple[float, float]]) -> tuple[list[Reason], list[Reason]]:
    """Returns (tier 1 reasons to send now, tier 2 reasons for the next batch)."""
    t1: list[Reason] = []
    t2: list[Reason] = []

    before = {k: dict(v) for k, v in (state.get("pullback") or {}).items() if v}
    side = pullback(r, state, cfg)
    if side and _cooled(state, f"pullback_{side}", now, cfg.pullback_cooldown_minutes):
        sign = 1 if side == "bull" else -1
        lowest = (before.get(side) or {}).get("dip")
        intra = r.stock.composites["intraday"][0]
        head = "PULLBACK OVER · call swing entry" if side == "bull" else "BOUNCE OVER · put swing entry"
        dipped = f"{'dipped' if side == 'bull' else 'bounced'} to {sign * lowest:+.0f}, " if lowest is not None else ""
        t1.append(Reason("pullback_entry", side, f"{head}: swing {WORD[r.stock.label]} ({r.stock.score:+.0f}) · "
                                                 f"intraday {dipped}back to {intra:+.0f}"))

    t1 += evaluate_strong(r, state, cfg, now)
    for x in [x for x in t1 if x.rule == "alignment_ended"]:
        t1.remove(x)
        t2.append(x)

    if r.stock and r.stock.timeframes:
        slot = state.get("swing", {})
        new = hysteresis(slot.get("cls"), r.stock.score, (cfg.trend_strong, cfg.change_enter),
                         (cfg.strong_exit, cfg.change_exit))
        ch = _confirmed_class(state, "swing", new, cfg.confirm_cycles)
        if ch:
            t2.append(Reason("trend_change", direction(ch[1]),
                             f"{WORD[ch[0]]} → {WORD[ch[1]]} ({r.stock.score:+.0f})"))

    if r.bias and r.bias.components and not (r.options and r.options.thin):
        slot = state.get("opts", {})
        new = _options_class(r.bias.score, cfg.options_enter, cfg.options_exit, slot.get("cls"))
        ch = _confirmed_class(state, "opts", new, cfg.options_confirm_cycles)
        if ch:
            prem = r.bias.session_net_premium
            extra = f" · net premium {'+' if prem >= 0 else '-'}${abs(prem) / 1e6:.1f}M" if prem else ""
            t2.append(Reason("options_flip", ch[1], f"options → {DIR_WORD[ch[1]]} ({r.bias.score:+.0f}){extra}"))

    if r.options:
        t2 += _iv_and_walls(r, state, cfg, now, iv_history)
    return t1, t2
