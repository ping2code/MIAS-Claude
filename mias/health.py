"""Health gate: a symbol whose data is incomplete or stale produces no signals (alerts, paper entries) rather than
misleading ones. The alert state is left untouched, so a change still fires once the data is clean again.

Checks, during the regular session:
  - the cycle's read of the symbol had errors (failed or truncated stock/options requests)
  - its 5-minute bars are stale (the last completed candle is more than STALE_MINUTES old)
  - few of its option contracts had a live quote (quote coverage below MIN_QUOTE_COVERAGE)
Paper entries also need a tight, fresh option quote (spread_issue, quote_issue).

Every held signal is counted per day (KEY:{date}) for the closing and weekly reports.
"""
from __future__ import annotations

import logging
from datetime import datetime, time

log = logging.getLogger(__name__)

STALE_MINUTES = 15
MIN_QUOTE_COVERAGE = 0.5
MAX_SPREAD_PCT = 10.0       # paper entries: bid/ask spread as a share of the mid
QUOTE_AGE_S = 120           # paper entries: the option quote must be this fresh
KEY = "health"


def symbol_issues(r, today5: list[dict] | None, now: datetime, tz) -> list[str]:
    """Why this symbol's data can't be trusted right now (empty when it can)."""
    out = [e for e in (r.errors or [])]
    local = now.astimezone(tz)
    if today5 is not None and local.time() >= time(9, 45):
        last = max((b["t"] for b in today5), default=None)
        if last is None:
            out.append("no 5-minute bars today")
        else:
            age = (now.timestamp() * 1000 - (last + 300_000)) / 60_000
            if age > STALE_MINUTES:
                out.append(f"5-minute bars stale ({age:.0f} min)")
    cov = getattr(r.options, "quote_coverage", None) if r.options else None
    if cov is not None and cov < MIN_QUOTE_COVERAGE:
        out.append(f"few live option quotes ({100 * cov:.0f}%)")
    return out


def spread_issue(bid: float | None, ask: float | None) -> str | None:
    if not bid or not ask:
        return "no two-sided option quote"
    pct = 100 * (ask - bid) / ((ask + bid) / 2)
    return f"option spread too wide ({pct:.0f}%)" if pct > MAX_SPREAD_PCT else None


def quote_issue(snap: dict, now: datetime) -> str | None:
    q = snap.get("last_quote") or {}
    if q.get("timeframe") and q["timeframe"] != "REAL-TIME":
        return f"option quote {q['timeframe'].lower()}"
    ts = q.get("last_updated")
    if ts:
        age = now.timestamp() - ts / 1e9
        if age > QUOTE_AGE_S:
            return f"option quote stale ({age:.0f} s)"
    return None


def record(store, now: datetime, tz, symbol: str, what: str, issues: list[str]) -> None:
    """Count a held signal for the day and log it."""
    day = now.astimezone(tz).date().isoformat()
    key = f"{KEY}:{day}"
    st = store.get(key) or {}
    reason = issues[0].split(":")[0].split("(")[0].strip()
    per = st.setdefault(symbol, {})
    per[reason] = per.get(reason, 0) + 1
    store.put(key, st)
    log.info("health gate: %s %s held: %s", symbol, what, "; ".join(issues))


def day_counts(store, day: str) -> dict[str, dict[str, int]]:
    return store.get(f"{KEY}:{day}") or {}


def summary_line(counts: dict[str, dict[str, int]]) -> str | None:
    if not counts:
        return None
    total = sum(sum(v.values()) for v in counts.values())
    parts = sorted(((sum(v.values()), s, max(v, key=v.get)) for s, v in counts.items()), reverse=True)
    return (f"🩺 Health gate held {total} signal{'s' if total != 1 else ''}: "
            + ", ".join(f"{s} ×{n} ({why})" for n, s, why in parts[:6]))
