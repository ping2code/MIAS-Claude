"""Early Watch v2 research report (report only), with v1 as the control column.

Definitions (docs/observer/V2_PREREGISTRATION.md section 7): signed returns at +5/15/30/60 min from the watch price;
MFE / MAE from the watch to its upgrade or end; "confirmation" = the first later confirmed event (any source, same
symbol and side) within 60 min of the watch; lead vs B = minutes to the first later B entry within 60 min; price
advantage = signed (confirmation price - watch price); pre-EMA = completed 5m EMA not yet crossed in the watch's
direction; session decomposition open -> watch -> confirmation -> +60 min; swing-leg decomposition leg start (latest
confirmed opposite pivot at the candidate) -> candidate -> watch -> confirmation -> +60 min.
"""
from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

from .report import Bars, enrich
from .rules import ONE

STRATS = {"B", "C", "D", "E7", "E21", "Simple"}
FAM_NAMES = {"structure": "V2-A Structure", "vwap": "V2-B VWAP", "or": "V2-C Opening range"}


def _src(s):
    return (s or "").replace("(replay)", "")


def _sg(side):
    return 1 if side == "bull" else -1


def _ret(sg, a, b):
    return None if a is None or b is None or not a else 100 * sg * (b / a - 1)


def _close_ms(t, tz):
    return int(datetime.fromtimestamp(t / 1000, timezone.utc).astimezone(tz)
               .replace(hour=16, minute=0, second=0, microsecond=0).timestamp() * 1000)


def outcomes(rows: list[dict], confirms: list[dict], bars: Bars, tz: ZoneInfo) -> list[dict]:
    """rows: {day, sym, side, t (watch ms), price, state, end_at, upgrade_at}. Adds returns, MFE/MAE, leads,
    confirmation price and session decomposition."""
    by = defaultdict(list)
    for c in confirms:
        by[(c["day"], c["sym"], c["side"])].append(c)
    for w in rows:
        sg, day, sym, t, px = _sg(w["side"]), w["day"], w["sym"], w["t"], w["price"]
        cm = _close_ms(t, tz)
        for n in (5, 15, 30, 60):
            w[f"r{n}"] = _ret(sg, px, bars.close_at(day, sym, t + n * ONE)) if t + n * ONE <= cm else None
        until = w.get("upgrade_at") or w.get("end_at") or t + 60 * ONE
        path = bars.path(day, sym, t, until)
        if path:
            w["mfe"] = max(100 * sg * ((r[2] if sg > 0 else r[3]) / px - 1) for r in path)
            w["mae"] = min(100 * sg * ((r[3] if sg > 0 else r[2]) / px - 1) for r in path)
        else:
            w["mfe"] = w["mae"] = None
        later = sorted((c for c in by[(day, sym, w["side"])] if t <= c["ts"] * 1000 <= t + 60 * ONE),
                       key=lambda c: c["ts"])
        b = next((c for c in later if _src(c["source"]) == "B"), None)
        w["lead_b"] = (b["ts"] * 1000 - t) / ONE if b else None
        first = later[0] if later else None
        w["lead_any"] = (first["ts"] * 1000 - t) / ONE if first else None
        w["confirm_src"] = _src(first["source"]) if first else None
        w["confirm_px"] = bars.close_at(day, sym, int(first["ts"] * 1000)) if first else None
        w["price_adv"] = _ret(sg, px, w["confirm_px"])
        srcs = {_src(c["source"]) for c in later}
        w["later"] = ("B" if "B" in srcs else "") + ("+CDE" if srcs & {"C", "D", "E7", "E21"} else "") + \
                     ("+alert" if srcs & {"swing_trend", "strong_alignment"} else "")
        w["later"] = w["later"].lstrip("+") or ("Simple" if "Simple" in srcs else "none")
        op = bars.open_px(day, sym)
        w["S_open_watch"] = _ret(sg, op, px)
        if first:
            ts = int(first["ts"] * 1000)
            w["S_watch_conf"] = w["price_adv"]
            w["S_conf_60"] = _ret(sg, w["confirm_px"], bars.close_at(day, sym, ts + 60 * ONE)) \
                if ts + 60 * ONE <= cm else None
        else:
            w["S_watch_conf"] = w["S_conf_60"] = None
    return rows


def _med(xs, n=2):
    xs = [x for x in xs if x is not None]
    return "–" if not xs else f"{median(xs):+.{n}f}"


def _medn(xs, n=1):
    xs = [x for x in xs if x is not None]
    return "–" if not xs else f"{median(xs):.{n}f}"


def _pct(k, n):
    return "–" if not n else f"{100 * k / n:.0f}%"


def summary(ws: list[dict]) -> dict:
    n = len(ws)
    st = Counter(w["state"] for w in ws)
    mfe, mae = [w["mfe"] for w in ws if w["mfe"] is not None], [w["mae"] for w in ws if w["mae"] is not None]
    ratio = (median(mfe) / abs(median(mae))) if mfe and mae and median(mae) else None
    return {"n": n, "inv": _pct(st["INVALIDATED"], n), "exp": _pct(st["EXPIRED"], n), "upg": _pct(st["UPGRADED"], n),
            "r5": _med([w["r5"] for w in ws], 3), "r15": _med([w["r15"] for w in ws], 3),
            "r30": _med([w["r30"] for w in ws], 3), "r60": _med([w["r60"] for w in ws], 3),
            "mfe": _med(mfe, 3), "mae": _med(mae, 3), "ratio": "–" if ratio is None else f"{ratio:.2f}",
            "lead_b": _medn([w["lead_b"] for w in ws]), "lead_any": _medn([w["lead_any"] for w in ws]),
            "adv": _med([w["price_adv"] for w in ws], 3),
            "pre": _pct(sum(1 for w in ws if w.get("pre_ema")), n) if ws and "pre_ema" in ws[0] else "n/a"}


def build_v2_report(db, tz: ZoneInfo, out_dir: Path, day: str | None = None, v1db=None, title: str = "") -> str:
    out_dir.mkdir(parents=True, exist_ok=True)
    bars = Bars(db)
    q = "day = ?" if day else "1=1"
    args = (day,) if day else ()
    confirms = [dict(zip(("day", "sym", "side", "ts", "source"), r))
                for r in db.db.execute(f"SELECT day, sym, side, ts, source FROM confirms WHERE {q}", args)]
    days = sorted({r[0] for r in db.db.execute(f"SELECT DISTINCT day FROM bars1 WHERE {q}", args)})
    watches = db.v2_watches(q, args)
    rows = [dict(w, t=w["watch_at"], price=w["watch_price"]) for w in watches]
    outcomes(rows, confirms, bars, tz)
    cross = defaultdict(list)
    for d, s, side, t in db.db.execute("SELECT day, sym, side, at FROM v2_ema_cross"):
        cross[(d, s, side)].append(t)
    for w in rows:
        nxt = [t for t in cross[(w["day"], w["sym"], w["side"])] if t >= w["t"]]
        w["min_to_cross"] = (min(nxt) - w["t"]) / ONE if nxt else None
        w["detect_min"] = (w["watch_at"] - w["candidate_at"]) / ONE
        w["tg_delay_s"] = (w["telegram_sent_at"] - w["watch_at"] / 1000) if w.get("telegram_sent_at") else None
        sg = _sg(w["side"])
        ls = (w.get("leg_start") or {}).get("price")
        w["L_start_cand"] = _ret(sg, ls, w["candidate_price"])
        w["L_cand_watch"] = _ret(sg, w["candidate_price"], w["watch_price"])
        w["L_watch_conf"], w["L_conf_60"] = w["S_watch_conf"], w["S_conf_60"]
    cands = db.db.execute(f"SELECT family, status, COUNT(*) FROM v2_candidate_log WHERE {q} GROUP BY family, status",
                          args).fetchall()
    ends = db.db.execute(f"SELECT family, end_status, COUNT(*) FROM v2_candidates WHERE {q} GROUP BY family, "
                         "end_status", args).fetchall()
    L = [f"# {title or 'Early Watch v2 research report'}", "",
         f"Days: {len(days)} ({days[0] if days else '–'} … {days[-1] if days else '–'}) · rules early-watch-v2-research "
         "(POST-HOC hypotheses, frozen at 934a2e2) · v1 = frozen control. Returns in % of the underlying, signed by "
         "side. Main lane = watches 09:45–15:00 ET.", ""]
    # funnel
    L += ["## 1. Funnel per family (candidate → watch → Telegram)", "",
          "| Family | Raw candidates | Pending → watched | → not confirmed | → stale | Blocked at trigger "
          "(open watch / pending / same leg / stale) | Watches (main / opening / late) | Telegram sent | merged | "
          "not sent | Detect delay min (candidate→watch, med) | Telegram delay s (med) |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for fam in ("structure", "vwap", "or"):
        cs = {s: n for f, s, n in cands if f == fam}
        es = {s: n for f, s, n in ends if f == fam}
        ws = [w for w in rows if w["family"] == fam]
        lanes = Counter(w["lane"] for w in ws)
        sent = sum(1 for w in ws if w["telegram_sent"])
        merged = sum(1 for w in ws if (w.get("telegram_error") or "").startswith("merged"))
        nots = sum(1 for w in ws if (w.get("telegram_error") or "").startswith("not sent"))
        L.append(f"| {FAM_NAMES[fam]} | {sum(cs.values())} | {es.get('watched', 0)} | {es.get('not_confirmed', 0)} | "
                 f"{es.get('stale', 0)} | {cs.get('open_watch_exists', 0)} / {cs.get('pending_exists', 0)} / "
                 f"{cs.get('same_leg', 0)} / {cs.get('stale', 0)} | {lanes['main']} / {lanes['opening']} / "
                 f"{lanes['late']} | {sent} | {merged} | {nots} | {_medn([w['detect_min'] for w in ws])} | "
                 f"{_medn([w['tg_delay_s'] for w in ws])} |")
    # comparison
    v1rows = []
    if v1db is not None:
        v1w, _ = enrich(v1db, tz, day)
        v1c = [dict(zip(("day", "sym", "side", "ts", "source"), r)) for r in
               v1db.db.execute(f"SELECT day, sym, side, ts, source FROM confirms WHERE {q}", args)]
        v1rows = outcomes([dict(day=w["day"], sym=w["sym"], side=w["side"], t=w["created_at"], price=w["price"],
                                state=w["state"], end_at=w["end_at"], upgrade_at=w["upgrade_at"],
                                telegram_sent=w.get("telegram_sent"))
                           for w in v1w if w["lane"] == "main"], v1c, Bars(v1db), tz)
        v1cand = len({(r[0], r[1], r[2], r[3]) for r in v1db.db.execute(
            f"SELECT day, sym, side, leg_id FROM watches WHERE lane='main' AND {q} UNION "
            f"SELECT day, sym, side, leg_id FROM suppressed WHERE lane='main' AND {q}", args + args)})
    main = {fam: [w for w in rows if w["family"] == fam and w["lane"] == "main"] for fam in ("structure", "vwap", "or")}
    cols = [("V1 (control)", v1rows)] + [(FAM_NAMES[f], main[f]) for f in main]
    sums = [summary(ws) for _, ws in cols]
    rawc = [str(v1cand) + " legs" if v1db is not None else "–"] + \
        [str(sum(n for f, s, n in cands if f == fam)) for fam in main]
    tg = [str(sum(1 for w in v1rows if w.get("telegram_sent")))] + \
        [str(sum(1 for w in main[f] if w["telegram_sent"])) for f in main]
    L += ["", "## 2. Comparison (main lane)", "",
          "| Metric | " + " | ".join(n for n, _ in cols) + " |", "|---|" + "---:|" * len(cols)]
    for label, key in (("Raw candidates", None), ("Watches", "n"), ("Telegram sent", "tg"),
                       ("Invalidated %", "inv"), ("Expired %", "exp"), ("Upgraded %", "upg"), ("5m return %", "r5"),
                       ("15m return %", "r15"), ("30m return %", "r30"), ("60m return %", "r60"), ("MFE %", "mfe"),
                       ("MAE %", "mae"), ("MFE / |MAE|", "ratio"), ("Lead vs B (min, med)", "lead_b"),
                       ("Lead vs any confirmed (min, med)", "lead_any"), ("Price advantage % (med)", "adv"),
                       ("Pre-EMA detection %", "pre")):
        if key is None:
            vals = rawc
        elif key == "tg":
            vals = tg
        else:
            vals = [str(s[key]) for s in sums]
        if key == "pre":
            vals[0] = "n/a (gate)"
        L.append(f"| {label} | " + " | ".join(vals) + " |")
    # EMA timing, decompositions, secondary
    L += ["", "## 3. Before EMA confirmation", "",
          "| Family | Watches | Pre-EMA | Minutes to next EMA cross (pre-EMA watches with a cross, med) | "
          "Pre-EMA watches never crossed that day |", "|---|---|---|---|---|"]
    for f, ws in main.items():
        pre = [w for w in ws if w["pre_ema"]]
        L.append(f"| {FAM_NAMES[f]} | {len(ws)} | {_pct(len(pre), len(ws))} | "
                 f"{_medn([w['min_to_cross'] for w in pre])} | {sum(1 for w in pre if w['min_to_cross'] is None)} |")
    L += ["", "## 4. Move decomposition (medians, %; confirmation = first later confirmed event within 60 min)", "",
          "| Family | n confirmed | SESSION open → watch | watch → confirmation | confirmation → +60 | "
          "SWING leg start → candidate | candidate → watch | watch → confirmation | confirmation → +60 |",
          "|---|---|---|---|---|---|---|---|---|"]
    for name, ws in cols:
        cf = [w for w in ws if w.get("confirm_px") is not None]
        L.append(f"| {name} | {len(cf)} | {_med([w['S_open_watch'] for w in cf], 3)} | "
                 f"{_med([w['S_watch_conf'] for w in cf], 3)} | {_med([w['S_conf_60'] for w in cf], 3)} | "
                 f"{_med([w.get('L_start_cand') for w in cf], 3)} | {_med([w.get('L_cand_watch') for w in cf], 3)} | "
                 f"{_med([w.get('L_watch_conf') for w in cf], 3)} | {_med([w.get('L_conf_60') for w in cf], 3)} |")
    L += ["", "## 5. What followed within 60 min (secondary)", "",
          "| Family | " + " | ".join(["B", "B+CDE", "CDE", "alert", "B+alert", "Simple", "none", "other"]) + " |",
          "|---|" + "---|" * 8]
    for name, ws in cols:
        c = Counter(w["later"] for w in ws)
        known = ["B", "B+CDE", "CDE", "alert", "B+alert", "Simple", "none"]
        L.append(f"| {name} | " + " | ".join(str(c.get(k, 0)) for k in known) +
                 f" | {sum(v for k, v in c.items() if k not in known)} |")
    # breakdowns + concentration
    L += ["", "## 6. Breakdowns (main lane)", ""]
    for f, ws in main.items():
        if not ws:
            continue
        L.append(f"### {FAM_NAMES[f]}")
        for label, key in (("side", lambda w: w["side"]), ("symbol", lambda w: w["sym"]),
                           ("hour", lambda w: datetime.fromtimestamp(w["t"] / 1000, timezone.utc).astimezone(tz)
                            .strftime("%H:00")),
                           ("SPY direction", lambda w: (w.get("context") or {}).get("spy_dir") or "n/a")):
            g = defaultdict(list)
            for w in ws:
                g[key(w)].append(w)
            L.append(f"- {label}: " + "; ".join(f"{k} n={len(v)} r60 {summary(v)['r60']} inv {summary(v)['inv']}"
                                                for k, v in sorted(g.items())))
        sc, dc = Counter(w["sym"] for w in ws), Counter(w["day"] for w in ws)
        ts, td = sc.most_common(1)[0][0], dc.most_common(1)[0][0]
        L.append(f"- concentration: top 3 symbols {100 * sum(n for _, n in sc.most_common(3)) / len(ws):.0f}% · "
                 f"top 3 days {100 * sum(n for _, n in dc.most_common(3)) / len(ws):.0f}% · r60 without top symbol "
                 f"{ts}: {summary([w for w in ws if w['sym'] != ts])['r60']} · without top day {td}: "
                 f"{summary([w for w in ws if w['day'] != td])['r60']}")
        per_day = Counter(w["day"] for w in ws)
        counts = [per_day.get(d, 0) for d in days]
        L.append(f"- main watches per day: median {median(counts) if counts else 0:.0f} · max {max(counts) if counts else 0}")
        L.append("")
    # chronological list
    L += ["## 7. Every v2 watch, in order", "",
          "| Watch ET | Family | Lane | Symbol | Side | Candidate ET @ | Watch @ | Level | Pre-EMA | Telegram | End | "
          "Reason | Lead B | Lead any | r15 | r60 |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    hm = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(tz).strftime("%H:%M")
    f2 = lambda x, n=2: "–" if x is None else f"{x:.{n}f}"
    for w in rows:
        tgs = "sent " + (w["telegram_message_id"] or "") if w["telegram_sent"] else (w.get("telegram_error") or "–")
        L.append(f"| {w['day'][5:]} {hm(w['t'])} | {w['family']} | {w['lane']} | {w['sym']} | {w['side']} | "
                 f"{hm(w['candidate_at'])} @ {f2(w['candidate_price'])} | {f2(w['watch_price'])} | {f2(w['level'])} | "
                 f"{'yes' if w['pre_ema'] else 'no'} | {tgs} | {w['state']} | {w['upgrade_source'] or w['end_reason']} |"
                 f" {f2(w['lead_b'], 1)} | {f2(w['lead_any'], 1)} | {f2(w['r15'], 3)} | {f2(w['r60'], 3)} |")
    text = "\n".join(L)
    (out_dir / "V2_REPORT.md").write_text(text)
    keys = ["watch_id", "family", "lane", "day", "sym", "side", "candidate_at", "candidate_price", "watch_at",
            "watch_price", "level", "pre_ema", "min_to_cross", "state", "end_reason", "upgrade_source", "lead_b",
            "lead_any", "confirm_src", "confirm_px", "price_adv", "r5", "r15", "r30", "r60", "mfe", "mae",
            "S_open_watch", "S_watch_conf", "S_conf_60", "L_start_cand", "L_cand_watch", "later", "telegram_sent",
            "telegram_message_id", "telegram_error", "detect_min", "tg_delay_s"]
    with (out_dir / "v2_watches.csv").open("w", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(keys + ["ema_watch", "context"])
        for w in rows:
            wr.writerow([w.get(k) for k in keys] + [json.dumps(w["ema_watch"]), json.dumps(w["context"])])
    return text
