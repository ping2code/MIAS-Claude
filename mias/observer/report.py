"""Scoring for Early Watch (report only). Reads one observer database (live or replay); writes a markdown report and
a CSV with every watch and its outcome.

Definitions (pre-registered, docs/observer/PREREGISTRATION.md section F):
  signed return  side sign x (close of the last 1-minute bar ending at or before t / watch price - 1)
  N-min return   at watch time + N (5, 15, 30, 60); empty if that is past 16:00
  MFE / MAE      best / worst signed excursion (1-minute highs / lows) from the watch until its upgrade, or until
                 it ended (invalidated / expired)
  lead time      upgrade time - watch time (minutes)
  price advantage  signed (confirmation price - watch price) / watch price: > 0 = the watch saw a better price
  confirmation price  close of the last 1-minute bar ending at or before the confirmed event
  move decomposition (PRIMARY reference = the regular-session open, the 09:30 1-minute bar's open):
                 A open -> watch, B watch -> confirmation, C confirmation -> +60 min, all signed by the watch side
  consumed at confirmation  (A + B) / (A + B + C), pooled over watches whose total A + B + C > 0
"""
from __future__ import annotations

import csv
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from zoneinfo import ZoneInfo

from .rules import ONE

STRATEGY = {"B", "C", "D", "E7", "E21", "Simple"}


def _strategy(src: str | None) -> str | None:
    if not src:
        return None
    base = src.replace("(replay)", "")
    return base if base in STRATEGY else None


class Bars:
    def __init__(self, db):
        self.by: dict[tuple, list[tuple]] = defaultdict(list)
        for day, sym, s, o, h, l, c in db.db.execute("SELECT day, sym, s, o, h, l, c FROM bars1 ORDER BY s"):
            self.by[(day, sym)].append((s, o, h, l, c))
        self.s_index = {k: [x[0] for x in v] for k, v in self.by.items()}

    def close_at(self, day, sym, ts_ms) -> float | None:
        import bisect
        rows = self.by.get((day, sym)) or []
        idx = bisect.bisect_right(self.s_index.get((day, sym)) or [], ts_ms - ONE) - 1
        return rows[idx][4] if idx >= 0 else None

    def open_px(self, day, sym) -> float | None:
        rows = self.by.get((day, sym)) or []
        return rows[0][1] if rows else None

    def path(self, day, sym, t0, t1):
        return [r for r in self.by.get((day, sym)) or [] if r[0] >= t0 and r[0] + ONE <= t1]


def _sgn(side):
    return 1 if side == "bull" else -1


def _ret(sign, a, b):
    return None if a is None or b is None or not a else 100 * sign * (b / a - 1)


def enrich(db, tz: ZoneInfo, day: str | None = None) -> tuple[list[dict], list[dict]]:
    bars = Bars(db)
    watches = db.watches("day = ?", (day,)) if day else db.watches()
    confirms = [dict(zip(("day", "sym", "side", "ts", "source"), r))
                for r in db.db.execute("SELECT day, sym, side, ts, source FROM confirms ORDER BY ts")
                if day is None or r[0] == day]
    by_key = defaultdict(list)
    for c in confirms:
        by_key[(c["day"], c["sym"], c["side"])].append(c)
    for w in watches:
        sg, day, sym = _sgn(w["side"]), w["day"], w["sym"]
        t = w["created_at"]
        close_ms = int(datetime.fromtimestamp(t / 1000, timezone.utc).astimezone(tz)
                       .replace(hour=16, minute=0, second=0, microsecond=0).timestamp() * 1000)
        for n in (5, 15, 30, 60):
            w[f"r{n}"] = _ret(sg, w["price"], bars.close_at(day, sym, t + n * ONE)) if t + n * ONE <= close_ms else None
        until = w["upgrade_at"] or w["end_at"] or t + 60 * ONE
        path = bars.path(day, sym, t, until)
        if path:
            fav = [100 * sg * ((r[2] if sg > 0 else r[3]) / w["price"] - 1) for r in path]
            adv = [100 * sg * ((r[3] if sg > 0 else r[2]) / w["price"] - 1) for r in path]
            w["mfe"], w["mae"] = max(fav), min(adv)
        else:
            w["mfe"] = w["mae"] = None
        w["lead_min"] = (w["upgrade_at"] - t) / ONE if w["state"] == "UPGRADED" else None
        w["price_adv"] = _ret(sg, w["price"], w["upgrade_price"]) if w["state"] == "UPGRADED" else None
        if w["state"] == "UPGRADED":
            op = bars.open_px(day, sym)
            w["A"] = _ret(sg, op, w["price"])
            w["B"] = w["price_adv"]
            after = bars.close_at(day, sym, w["upgrade_at"] + 60 * ONE)
            w["C"] = _ret(sg, w["upgrade_price"], after) if w["upgrade_at"] + 60 * ONE <= close_ms else None
        else:
            w["A"] = w["B"] = w["C"] = None
        later = [c for c in by_key[(day, sym, w["side"])] if t <= c["ts"] * 1000 <= t + 60 * ONE]
        strat = sorted({s for s in (_strategy(c["source"]) for c in later) if s})
        w["strategy_entry"] = bool(strat)
        w["strategies"] = "+".join(strat)
        hh = datetime.fromtimestamp(t / 1000, timezone.utc).astimezone(tz)
        w["hour"] = f"{hh:%H}:00"
    # every confirmed strategy entry: how much of the move had happened by then (open -> confirm -> +60 min)
    ce = []
    for c in confirms:
        if not _strategy(c["source"]):
            continue
        sg, ts = _sgn(c["side"]), int(c["ts"] * 1000)
        px = bars.close_at(c["day"], c["sym"], ts)
        op = bars.open_px(c["day"], c["sym"])
        after = bars.close_at(c["day"], c["sym"], ts + 60 * ONE)
        close_ms = int(datetime.fromtimestamp(ts / 1000, timezone.utc).astimezone(tz)
                       .replace(hour=16, minute=0, second=0, microsecond=0).timestamp() * 1000)
        prior = [w for w in watches if w["day"] == c["day"] and w["sym"] == c["sym"] and w["side"] == c["side"]
                 and w["created_at"] <= ts <= w["created_at"] + 60 * ONE]
        ce.append({**c, "px": px, "A": _ret(sg, op, px),
                   "C": _ret(sg, px, after) if ts + 60 * ONE <= close_ms else None,
                   "had_watch_main": any(w["lane"] == "main" for w in prior),
                   "had_watch_any": bool(prior)})
    return watches, ce


# ---- formatting ---------------------------------------------------------------------------------------

def _m(xs, f=median, n=2):
    xs = [x for x in xs if x is not None]
    return "–" if not xs else f"{f(xs):+.{n}f}" if f is not len else str(len(xs))


def _rate(k, n):
    return "–" if not n else f"{100 * k / n:.0f}%"


def _summary_row(name, ws):
    n = len(ws)
    up = sum(w["state"] == "UPGRADED" for w in ws)
    inv = sum(w["state"] == "INVALIDATED" for w in ws)
    exp = sum(w["state"] == "EXPIRED" for w in ws)
    pos60 = [w["r60"] for w in ws if w["r60"] is not None]
    lead = [w["lead_min"] for w in ws if w["lead_min"] is not None]
    return (f"| {name} | {n} | {up} ({_rate(up, n)}) | {inv} ({_rate(inv, n)}) | {exp} ({_rate(exp, n)}) | "
            f"{(f'{median(lead):.0f}') if lead else '–'} | {_m([w['price_adv'] for w in ws])} | "
            f"{_m([w['r5'] for w in ws])} | {_m([w['r15'] for w in ws])} | {_m([w['r30'] for w in ws])} | "
            f"{_m([w['r60'] for w in ws])} | {_rate(sum(x > 0 for x in pos60), len(pos60))} | "
            f"{_m([w['mfe'] for w in ws])} | {_m([w['mae'] for w in ws])} |")


HEAD = ("| Group | Watches | Upgraded | Invalidated | Expired | Lead min (med) | Price adv % (med) | r5 % | r15 % | "
        "r30 % | r60 % | r60 > 0 | MFE % | MAE % |\n|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")


def _table(groups):
    return [HEAD] + [_summary_row(k, v) for k, v in groups if v]


def _by(ws, key):
    g = defaultdict(list)
    for w in ws:
        g[key(w)].append(w)
    return sorted(g.items(), key=lambda kv: str(kv[0]))


def _bucket(x, edges, labels):
    if x is None:
        return "n/a"
    for e, lab in zip(edges, labels):
        if x < e:
            return lab
    return labels[-1]


def _hm(ms, tz):
    return "–" if not ms else datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(tz).strftime("%H:%M:%S")


def _f(x, n=2):
    return "–" if x is None else f"{x:.{n}f}"


def chronological(watches: list[dict], tz: ZoneInfo) -> list[str]:
    L = ["| # | Watch time ET | Lane | Symbol | Side | Type | Price | Invalid level | VWAP | EMA9 / EMA21 | Telegram | "
         "End | End time | Reason / upgrade | Upgrade price | Lead min | r15 % | r60 % |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, w in enumerate(sorted(watches, key=lambda w: (w["created_at"], w["sym"])), 1):
        tg = ("sent " + (w.get("telegram_message_id") or "")) if w.get("telegram_sent") else \
            ("FAILED: " + w["telegram_error"] if w.get("telegram_error") and "not sent" not in w["telegram_error"]
             else (w.get("telegram_error") or ("log only" if w["lane"] != "main" else "not sent")))
        L.append(f"| {i} | {_hm(w['created_at'], tz)} | {w['lane']} | {w['sym']} | {w['side']} | "
                 f"{w['structure_type']} | {_f(w['price'])} | {_f(w['level'])} | {_f(w['vwap'])} | "
                 f"{_f(w['ema9'])} / {_f(w['ema21'])} | {tg} | {w['state']} | {_hm(w['end_at'], tz)} | "
                 f"{w['upgrade_source'] or w['end_reason'] or '–'} | {_f(w['upgrade_price'])} | "
                 f"{_f(w.get('lead_min'), 1)} | {_f(w.get('r15'), 3)} | {_f(w.get('r60'), 3)} |")
    return L


def build(db, tz: ZoneInfo, title: str, out_dir: Path, day: str | None = None) -> str:
    out_dir.mkdir(parents=True, exist_ok=True)
    watches, ce = enrich(db, tz, day)
    days = sorted({r[0] for r in db.db.execute("SELECT DISTINCT day FROM bars1") if day is None or r[0] == day})
    main = [w for w in watches if w["lane"] == "main"]
    opening = [w for w in watches if w["lane"] == "opening"]
    supp = db.db.execute("SELECT lane, reason, COUNT(*) FROM suppressed WHERE ? IS NULL OR day = ? "
                         "GROUP BY lane, reason ORDER BY lane, COUNT(*) DESC", (day, day)).fetchall()
    cols = ["watch_id", "day", "lane", "sym", "side", "structure_type", "leg_id", "created_at", "hour",
            "minutes_since_open", "price", "level", "vwap", "ema9", "ema21", "atr", "state", "end_reason", "end_at",
            "upgrade_source", "upgrade_at", "upgrade_price", "lead_min", "price_adv", "r5", "r15", "r30", "r60",
            "mfe", "mae", "A", "B", "C", "strategy_entry", "strategies", "opening_shadow_only", "or_break"]
    with (out_dir / "watches.csv").open("w", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(cols + ["context"])
        for w in watches:
            wr.writerow([w.get(c) for c in cols] + [w["context"]])
    L = [f"# {title}", "", f"Days: {len(days)} ({days[0] if days else '–'} … {days[-1] if days else '–'}) · "
         f"symbols: {len({w['sym'] for w in watches}) or '–'} with watches · rules: early-watch-v1 (frozen)", "",
         "All returns are signed by the watch side, in % of the underlying (not option P/L). "
         "Main lane = actionable shadow watches 09:45–15:00; opening lane = 09:30–09:45 research-only "
         "(opening_shadow_only). Every watch is in watches.csv.", ""]
    # 1 overview
    L += ["## 1. Overview", ""] + _table([("Main lane", main), ("Opening lane (shadow only)", opening)]) + [""]
    sent = [w for w in main if w.get("telegram_sent")]
    failed = [w for w in main if w.get("telegram_error") and "not sent" not in (w.get("telegram_error") or "")]
    stale = [w for w in main if "not sent" in (w.get("telegram_error") or "")]
    if any(w.get("telegram_sent") or w.get("telegram_error") for w in watches):
        L += ["### Telegram pilot", "",
              f"- main-lane watches: {len(main)} · sent {len(sent)} · failed {len(failed)} · not sent (found late, e.g. "
              f"restart backfill) {len(stale)} · opening-lane candidates (log only) {len(opening)}"]
        L += [f"  - failure: {w['watch_id']}: {w['telegram_error']}" for w in failed] + [""]
    # 2 frequency
    per_day = Counter(w["day"] for w in main)
    counts = [per_day.get(d, 0) for d in days]
    per_sym_day = Counter((w["day"], w["sym"]) for w in main)
    L += ["## 2. Frequency / noise (main lane)", "",
          f"- watches per day: mean {mean(counts):.1f} · median {median(counts):.0f} · max {max(counts)} · "
          f"days with none {sum(c == 0 for c in counts)} of {len(days)}" if days else "- no days",
          f"- per symbol per day (when a symbol had any): mean "
          f"{mean(per_sym_day.values()):.2f} · max {max(per_sym_day.values())}" if per_sym_day else "- –", ""]
    # terminology (2026-11-11): raw candidate legs -> suppressed -> watch events created -> Telegram sent
    legs = db.db.execute("SELECT lane, day, sym, side, leg_id FROM watches WHERE ? IS NULL OR day = ? UNION "
                         "SELECT lane, day, sym, side, leg_id FROM suppressed WHERE ? IS NULL OR day = ?",
                         (day, day, day, day)).fetchall()
    watched = {(w["lane"], w["day"], w["sym"], w["side"], w["leg_id"]) for w in watches}
    never = [k for k in legs if k not in watched and k[0] == "main"]
    first = Counter()
    for k in never:
        rs = [r[0] for r in db.db.execute("SELECT reason FROM suppressed WHERE lane=? AND day=? AND sym=? AND side=? "
                                          "AND leg_id=? ORDER BY at", k)]
        first[rs[0] if rs else "?"] += 1
    L += ["Funnel (main lane): RAW CANDIDATE LEGS (symbol / side / leg on which the criteria held on 2 closes) → "
          "WATCH EVENTS CREATED → TELEGRAM SENT. Suppression records count each lane / symbol / side / leg / reason "
          "once; a leg can be suppressed and still be watched later.", "",
          f"- raw candidate legs {sum(1 for k in legs if k[0] == 'main')} → watch events created {len(main)} → "
          f"Telegram sent {sum(1 for w in main if w.get('telegram_sent'))}",
          f"- legs never watched {len(never)}, by first recorded reason: "
          + ", ".join(f"{r} {n}" for r, n in first.most_common()), ""]
    L += ["| Symbol | Main watches | per day | Upgraded | Invalidated |", "|---|---|---|---|---|"]
    sym_c = Counter(w["sym"] for w in main)
    for s, n in sym_c.most_common():
        ws = [w for w in main if w["sym"] == s]
        L.append(f"| {s} | {n} | {n / len(days):.2f} | {sum(w['state'] == 'UPGRADED' for w in ws)} | "
                 f"{sum(w['state'] == 'INVALIDATED' for w in ws)} |")
    L += ["", "| Hour (ET) | Main watches | per day |", "|---|---|---|"]
    for h, ws in _by(main, lambda w: w["hour"]):
        L.append(f"| {h} | {len(ws)} | {len(ws) / len(days):.2f} |")
    L += ["", "Suppressed candidates (criteria met but no watch), counted once per symbol / side / leg / reason:", "",
          "| Lane | Reason | Count |", "|---|---|---|"] + [f"| {a} | {b} | {c} |" for a, b, c in supp] + [""]
    # 3 end states
    L += ["## 3. How watches ended", ""]
    for lane, ws in (("main", main), ("opening", opening)):
        rc = Counter((w["state"], w["end_reason"]) for w in ws)
        L.append(f"- {lane}: " + (", ".join(f"{s} / {r}: {n}" for (s, r), n in rc.most_common()) or "none"))
    inv_sym = Counter(w["sym"] for w in main if w["state"] == "INVALIDATED")
    tot_inv = sum(inv_sym.values())
    if tot_inv:
        top = inv_sym.most_common(3)
        L.append(f"- invalidation concentration (main): top 3 symbols {', '.join(f'{s} {n}' for s, n in top)} = "
                 f"{100 * sum(n for _, n in top) / tot_inv:.0f}% of {tot_inv}")
    L.append("")
    # 4 conversion / lead / price advantage
    up = [w for w in main if w["state"] == "UPGRADED"]
    L += ["## 4. Conversion, lead time, price advantage (main lane)", "",
          f"- upgraded {len(up)} of {len(main)} ({_rate(len(up), len(main))})",
          f"- lead time: median {_m([w['lead_min'] for w in up], n=1)} min · mean "
          f"{_m([w['lead_min'] for w in up], mean, 1)} min",
          f"- price advantage (watch vs confirmation, signed %): median {_m([w['price_adv'] for w in up], n=3)} · "
          f"mean {_m([w['price_adv'] for w in up], mean, 3)} · positive in "
          f"{_rate(sum((w['price_adv'] or 0) > 0 for w in up), len(up))}", ""]
    L += _table(_by(up, lambda w: f"upgraded by {w['upgrade_source']}")) + [""]
    # 5 decomposition
    dec = [w for w in up if None not in (w["A"], w["B"], w["C"])]
    pos = [w for w in dec if w["A"] + w["B"] + w["C"] > 0]
    share = (sum(w["A"] + w["B"] for w in pos) / sum(w["A"] + w["B"] + w["C"] for w in pos)) if pos else None
    L += ["## 5. Move decomposition (upgraded main watches; reference = regular-session open)", "",
          f"- n = {len(dec)} with a full +60 min after confirmation",
          f"- A open → watch: median {_m([w['A'] for w in dec], n=3)} % · B watch → confirmation: median "
          f"{_m([w['B'] for w in dec], n=3)} % · C confirmation → +60 min: median {_m([w['C'] for w in dec], n=3)} %",
          f"- consumed by confirmation (pooled, totals > 0, n = {len(pos)}): "
          f"{'–' if share is None else f'{100 * share:.0f}%'}", ""]
    ce_ok = [c for c in ce if c["A"] is not None and c["C"] is not None]
    L += ["All confirmed strategy entries on these days (with or without a watch):", "",
          "| Group | Entries | open → entry % (med) | entry → +60 % (med) | +60 > 0 |", "|---|---|---|---|---|"]
    for name, grp in (("all", ce_ok), ("had a main watch in the prior 60 min", [c for c in ce_ok if c["had_watch_main"]]),
                      ("no watch", [c for c in ce_ok if not c["had_watch_any"]])):
        L.append(f"| {name} | {len(grp)} | {_m([c['A'] for c in grp], n=3)} | {_m([c['C'] for c in grp], n=3)} | "
                 f"{_rate(sum(c['C'] > 0 for c in grp), len(grp))} |")
    cov = [c for c in ce if True]
    L += ["", f"- confirmed strategy entries preceded by a main watch (same symbol/side, within 60 min): "
              f"{sum(c['had_watch_main'] for c in cov)} of {len(cov)} ({_rate(sum(c['had_watch_main'] for c in cov), len(cov))})",
          ""]
    # 6 opening lane
    L += ["## 6. 09:30–09:45 opening lane (research only) vs main", ""] + \
        _table([("Opening lane", opening), ("Main lane", main),
                ("Main, first 30 min (09:45–10:15)", [w for w in main if w["minutes_since_open"] <= 45])]) + [""]
    # 7 structure type
    L += ["## 7. Developing HL/LH vs opening-range break (main lane)", ""] + \
        _table(_by(main, lambda w: {"HL": "developing HL/LH", "LH": "developing HL/LH"}.get(w["structure_type"],
                                                                                           "opening-range break")))
    L += [""]
    # 8 breakdowns
    L += ["## 8. Breakdowns (main lane; context fields were not gates)", ""]
    for name, key in (
            ("Side", lambda w: w["side"]),
            ("Minutes since open", lambda w: _bucket(w["minutes_since_open"], [45, 90, 180, 270],
                                                     ["<=45", "45-90", "90-180", "180-270", ">270"])),
            ("SPY direction at watch", lambda w: w["context"].get("spy_dir") or "n/a"),
            ("Relative volume", lambda w: _bucket(w["context"].get("rel_vol"), [0.8, 1.2, 2.0],
                                                  ["<0.8", "0.8-1.2", "1.2-2.0", ">=2.0"])),
            ("SMA200 side", lambda w: w["context"].get("sma200_side") or "n/a"),
            ("RSI(14, 5m)", lambda w: _bucket(w["context"].get("rsi14_5m"), [30, 50, 70], ["<30", "30-50", "50-70",
                                                                                          ">=70"])),
            ("MACD hist sign vs side", lambda w: "n/a" if w["context"].get("macd_hist_5m") is None else
             ("with side" if _sgn(w["side"]) * w["context"]["macd_hist_5m"] > 0 else "against side")),
            ("Options bias at watch", lambda w: w["context"].get("options_bias") or "n/a"),
            ("Eventual strategy entry (60 min)", lambda w: w["strategies"] or "none"),
            ("Symbol", lambda w: w["sym"])):
        L += [f"### {name}", ""] + _table(_by(main, key)) + [""]
    # 9 concentration
    L += ["## 9. Concentration / robustness (main lane)", ""]
    if main:
        tops = sym_c.most_common(1)[0][0]
        topd = per_day.most_common(1)[0][0]
        L += _table([("all", main), (f"without top symbol {tops}", [w for w in main if w["sym"] != tops]),
                     (f"without top day {topd}", [w for w in main if w["day"] != topd])])
        top3 = sum(n for _, n in sym_c.most_common(3))
        top3d = sum(n for _, n in per_day.most_common(3))
        L += ["", f"- top 3 symbols = {100 * top3 / len(main):.0f}% of main watches; top 3 days = "
                  f"{100 * top3d / len(main):.0f}%"]
    L += [""]
    L += ["## 10. Every watch, in order", ""] + chronological(watches, tz) + [""]
    text = "\n".join(L)
    (out_dir / "REPORT.md").write_text(text)
    return text
