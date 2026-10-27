"""Open option positions per Telegram user, valued each cycle from live leg quotes, with private alerts.

  /buy AMZN 11/20 260/280C 6.63 x1 [by 10/22]   debit spread: first strike is the long leg
  /buy NVDA 12/18 190C 9.85 x2                   single leg
  /sell AMZN [price]   or   /sell 3 [price]      close (the price defaults to the current mid)
  /positions                                     open positions with live value and P&L

Alerts (each once per position, to the owner's private chat): take-profit reached, stop-loss hit, underlying
near the short strike, time stop due, hard exit before expiry, and the swing trend turning against the position.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import escape

from .picks import add_trading_days

log = logging.getLogger(__name__)

KEY = "positions"
CLOSED_KEY = "positions_closed"
STRIKES = re.compile(r"^(\d+(?:\.\d+)?)(?:/(\d+(?:\.\d+)?))?([CP])$")
QTY = re.compile(r"^X?(\d+)$")


class PositionError(ValueError):
    pass


@dataclass
class Leg:
    ticker: str
    strike: float


def _expiry(text: str, today: date) -> date:
    try:
        if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
            return date.fromisoformat(text)
        parts = [int(x) for x in text.split("/")]
        if len(parts) == 3:
            return date(parts[2] + (2000 if parts[2] < 100 else 0), parts[0], parts[1])
        if len(parts) == 2:
            d = date(today.year, parts[0], parts[1])
            return d if d >= today else date(today.year + 1, parts[0], parts[1])
    except ValueError:
        pass
    raise PositionError(f"can't read the expiry {text!r}: use MM/DD, e.g. 11/20")


def option_ticker(symbol: str, expiry: date, kind: str, strike: float) -> str:
    return f"O:{symbol}{expiry:%y%m%d}{kind[0].upper()}{round(strike * 1000):08d}"


def parse_buy(args: list[str], today: date) -> dict:
    """args as the bot passes them (upper case): SYMBOL EXPIRY STRIKES PRICE [xQTY] [BY MM/DD]."""
    usage = "Use: <code>/buy AMZN 11/20 260/280C 6.63 x1</code> (first strike = the one you bought)"
    if len(args) < 4:
        raise PositionError(usage)
    symbol, exp, strikes, price, *rest = args
    m = STRIKES.match(strikes)
    if not m:
        raise PositionError(f"can't read the strikes {strikes!r}. {usage}")
    try:
        entry = float(price.lstrip("$@"))
    except ValueError:
        raise PositionError(f"can't read the price {price!r}. {usage}") from None
    qty, exit_by = 1, None
    i = 0
    while i < len(rest):
        if rest[i] == "BY" and i + 1 < len(rest):
            exit_by = _expiry(rest[i + 1], today)
            i += 2
            continue
        q = QTY.match(rest[i])
        if not q:
            raise PositionError(f"can't read {rest[i]!r}. {usage}")
        qty = int(q.group(1))
        i += 1
    expiry = _expiry(exp, today)
    if expiry < today:
        raise PositionError("that expiry is in the past")
    kind = "call" if m.group(3) == "C" else "put"
    long_k = float(m.group(1))
    short_k = float(m.group(2)) if m.group(2) else None
    if short_k is not None:
        if short_k == long_k:
            raise PositionError("the two strikes are the same")
        if (kind == "call") != (short_k > long_k):
            raise PositionError("for a debit spread the strike you sell is further out of the money: "
                                "260/280C or 710/670P")
    if entry <= 0 or qty <= 0:
        raise PositionError("price and quantity must be positive")
    if short_k is not None and entry >= abs(short_k - long_k):
        raise PositionError(f"a {abs(short_k - long_k):g}-wide spread can't cost {entry:.2f}")
    return {"symbol": symbol.lstrip("$"), "kind": kind, "expiry": expiry.isoformat(), "long": long_k,
            "short": short_k, "entry": entry, "qty": qty, "opened": today.isoformat(),
            "exit_by": exit_by.isoformat() if exit_by else None}


def legs(p: dict) -> list[Leg]:
    exp = date.fromisoformat(p["expiry"])
    out = [Leg(option_ticker(p["symbol"], exp, p["kind"], p["long"]), p["long"])]
    if p.get("short") is not None:
        out.append(Leg(option_ticker(p["symbol"], exp, p["kind"], p["short"]), p["short"]))
    return out


def name(p: dict) -> str:
    exp = date.fromisoformat(p["expiry"])
    k = "C" if p["kind"] == "call" else "P"
    strikes = f"{p['long']:g}/{p['short']:g}{k}" if p.get("short") is not None else f"{p['long']:g}{k}"
    return f"{p['symbol']} {exp:%m/%d} {strikes}" + (f" ×{p['qty']}" if p.get("qty", 1) != 1 else "")


def _mid(snap: dict) -> float | None:
    q = snap.get("last_quote") or {}
    if q.get("bid") and q.get("ask"):
        return (q["bid"] + q["ask"]) / 2
    return q.get("midpoint") or (snap.get("session") or {}).get("close")


async def quote(client, p: dict) -> tuple[float | None, float | None]:
    """(position value per unit, underlying price) from live leg quotes."""
    ls = legs(p)
    snaps = await client.snapshots([leg.ticker for leg in ls])
    mids = [_mid(snaps.get(leg.ticker) or {}) for leg in ls]
    if any(m is None for m in mids):
        return None, None
    value = mids[0] - (mids[1] if len(mids) > 1 else 0.0)
    under = None
    for leg in ls:
        under = under or ((snaps.get(leg.ticker) or {}).get("underlying_asset") or {}).get("price")
    return round(value, 2), under


def max_profit(p: dict) -> float | None:
    if p.get("short") is None:
        return None
    return abs(p["short"] - p["long"]) - p["entry"]


def pnl(p: dict, value: float) -> float:
    return round((value - p["entry"]) * 100 * p.get("qty", 1), 2)


def _money(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}${abs(x):,.0f}"


def status_line(p: dict, value: float | None, under: float | None) -> str:
    if value is None:
        return f"{escape(name(p))}: no quote"
    line = f"{escape(name(p))} · now {value:.2f} (entry {p['entry']:.2f}) · {_money(pnl(p, value))}"
    mp = max_profit(p)
    if mp:
        line += f" · {100 * (value - p['entry']) / mp:.0f}% of max"
    if under:
        line += f" · {p['symbol']} {under:,.2f}"
    return line


# ---- rules ---------------------------------------------------------------------------------------

def check_rules(p: dict, value: float, under: float | None, today: date, rules,
                swing_change: tuple[str, str, float] | None = None) -> list[tuple[str, str]]:
    """New (rule, message) alerts for one position; marks them fired in p['fired']."""
    from .trend import direction
    fired = set(p.setdefault("fired", []))
    out = []
    e, mp = p["entry"], max_profit(p)
    head = escape(name(p))
    status = f"Now {value:.2f} (entry {e:.2f}) · {_money(pnl(p, value))}"
    exp = date.fromisoformat(p["expiry"])
    days_left = (exp - today).days

    def add(rule, text):
        if rule not in fired:
            fired.add(rule)
            out.append((rule, text))

    target = e + rules.take_profit * mp if mp else e * (1 + rules.single_take_profit)
    if value >= target:
        share = f"{100 * (value - e) / mp:.0f}% of max" if mp else f"+{100 * (value / e - 1):.0f}%"
        add("take_profit", f"🎯 <b>{head}: TAKE-PROFIT REACHED</b>\n{status} · {share} (target {target:.2f})\n"
                           f"Plan says: close it. {days_left} days left.")
    if value <= e * (1 - rules.stop_loss):
        add("stop_loss", f"🛑 <b>{head}: STOP-LOSS</b>\n{status} · down {100 * (1 - value / e):.0f}% "
                         f"(stop at -{100 * rules.stop_loss:.0f}%)\nPlan says: close it rather than hope.")
    if under and p.get("short") is not None:
        k, pct = p["short"], rules.near_strike_pct / 100
        near = under >= k * (1 - pct) if p["kind"] == "call" else under <= k * (1 + pct)
        if near:
            gap = 100 * abs(under / k - 1)
            where = "past" if (under >= k if p["kind"] == "call" else under <= k) else f"{gap:.1f}% from"
            add("near_short", f"🔔 <b>{head}: NEAR SHORT STRIKE</b>\n{p['symbol']} {under:,.2f}, {where} your "
                              f"{k:g} short {p['kind']}\n{status}"
                              + (f" · {100 * (value - e) / mp:.0f}% of max" if mp else "")
                              + "\nMost of the gain is in; the rest comes slowly. Consider taking it.")
    stop_day = date.fromisoformat(p["exit_by"]) if p.get("exit_by") else \
        add_trading_days(date.fromisoformat(p["opened"]), rules.time_stop_days)
    if today >= stop_day:
        add("time_stop", f"⏰ <b>{head}: TIME STOP</b> ({stop_day:%a %b %d})\n{status}\n"
                         f"Planned exit date reached. Close it, or extend on purpose.")
    if days_left <= rules.hard_exit_days:
        add("hard_exit", f"⚠️ <b>{head}: {days_left} DAYS TO EXPIRY</b>\n{status}\n"
                         f"Hard exit: close before expiry week (gamma and assignment risk).")
    if swing_change:
        old, new, score = swing_change
        want = "bull" if p["kind"] == "call" else "bear"
        o, n = direction(old), direction(new)
        if (o == want and n != want) or (n not in (want, "neutral") and o != n):
            from .signals import WORD
            out.append(("trend_against", f"❗ <b>{head}: TREND TURNED AGAINST YOU</b>\n{p['symbol']} swing "
                                         f"{WORD[old]} → {WORD[new]} ({score:+.0f})" +
                                         (f" · price {under:,.2f}" if under else "") + f"\n{status}"))
    p["fired"] = sorted(fired)
    return out


async def check_all(client, store, cfg, now: datetime, swing_changes: dict[str, tuple[str, str, float]]) -> int:
    """Value every open position and queue its alerts to the owner's private chat. Returns alerts queued."""
    positions = store.get(KEY, [])
    if not positions:
        return 0
    today = now.astimezone(cfg.tz).date()
    sent = 0
    for p in positions:
        try:
            value, under = await quote(client, p)
        except Exception as exc:
            log.warning("position %s quote failed: %s", name(p), exc)
            continue
        if value is None:
            continue
        p["last"] = {"value": value, "under": under, "ts": now.timestamp()}
        for rule, text in check_rules(p, value, under, today, cfg.positions, swing_changes.get(p["symbol"])):
            store.enqueue(f"position_{rule}", text, p["symbol"], chat=p["user"])
            sent += 1
    store.put(KEY, positions)
    return sent


# ---- commands ------------------------------------------------------------------------------------

def open_position(store, user: str, args: list[str], today: date) -> dict:
    p = parse_buy(args, today)
    positions = store.get(KEY, [])
    p["id"] = max([x["id"] for x in positions] + [x["id"] for x in store.get(CLOSED_KEY, [])] + [0]) + 1
    p["user"] = user
    positions.append(p)
    store.put(KEY, positions)
    return p


def find_open(store, user: str, ref: str) -> list[dict]:
    mine = [p for p in store.get(KEY, []) if p["user"] == user]
    if ref.isdigit():
        return [p for p in mine if p["id"] == int(ref)]
    return [p for p in mine if p["symbol"] == ref.lstrip("$")]


def close_position(store, p: dict, price: float, today: date) -> dict:
    positions = [x for x in store.get(KEY, []) if x["id"] != p["id"]]
    store.put(KEY, positions)
    closed = dict(p, exit=price, closed=today.isoformat(), pnl=pnl(p, price))
    store.put(CLOSED_KEY, (store.get(CLOSED_KEY, []) + [closed])[-200:])
    return closed


def mine(store, user: str) -> list[dict]:
    return [p for p in store.get(KEY, []) if p["user"] == user]
