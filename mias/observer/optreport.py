"""Early Watch options-context study (report only, added 2026-11-15). MEASUREMENT ONLY: nothing here changes, filters
or tunes a watch, and no threshold is to be changed from a few days of results.

Only contexts stored at watch time are used ("live snapshot", mias.observer.optctx). Watches recorded before the
context existed are shown as "not recorded"; they are never reconstructed from newer data. Any future post-hoc
reconstruction must carry source "post-hoc reconstructed" and is grouped separately (the source is part of every
group key below)."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from statistics import median
from zoneinfo import ZoneInfo

from .delivery import HORIZONS
from .optctx import INDEX

NOT_RECORDED = "not recorded"
ORDER = ["CONFIRMING", "NEUTRAL", "CONFLICTING", "BULLISH_DIVERGENCE", "BEARISH_DIVERGENCE", "INSUFFICIENT_DATA",
         NOT_RECORDED]
TOD = (("09:45–10:30", 9 * 60 + 45, 10 * 60 + 30), ("10:30–12:00", 10 * 60 + 30, 12 * 60),
       ("12:00–13:30", 12 * 60, 13 * 60 + 30), ("13:30–15:00", 13 * 60 + 30, 15 * 60))


def optx(w: dict) -> dict | None:
    return (w.get("context") or {}).get("optx")


def source(w: dict) -> str:
    x = optx(w)
    return x.get("source", "live snapshot") if x else NOT_RECORDED


def klass(w: dict) -> str:
    x = optx(w)
    return x["class"][w["side"]] if x else NOT_RECORDED


def overall_of(w: dict) -> str:
    x = optx(w)
    return x["overall"][w["side"]] if x else NOT_RECORDED


def _key(w, f):
    s = source(w)
    v = f(w)
    return v if s in (NOT_RECORDED, "live snapshot") else f"{v} [{s}]"


def _f(x, n=3):
    return "–" if x is None else f"{x:+.{n}f}"


def _med(xs):
    xs = [x for x in xs if x is not None]
    return median(xs) if xs else None


def _pct(ws, n):
    xs = [w.get(f"r{n}") for w in ws if w.get(f"r{n}") is not None]
    return f"{100 * sum(x > 0 for x in xs) / len(xs):.0f}% ({len(xs)})" if xs else "–"


def group_table(groups: list[tuple[str, list[dict]]]) -> list[str]:
    L = ["| Group | n | 5m correct | 10m | 15m | 30m | 60m | med r15 % | med r60 % | MFE15 % | MAE15 % | MFE60 % | "
         "MAE60 % |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for name, ws in groups:
        if not ws:
            continue
        L.append(f"| {name} | {len(ws)} | " + " | ".join(_pct(ws, n) for n in HORIZONS) +
                 f" | {_f(_med([w.get('r15') for w in ws]))} | {_f(_med([w.get('r60') for w in ws]))} | "
                 f"{_f(_med([w.get('mfe_15') for w in ws]))} | {_f(_med([w.get('mae_15') for w in ws]))} | "
                 f"{_f(_med([w.get('mfe_60') for w in ws]))} | {_f(_med([w.get('mae_60') for w in ws]))} |")
    return L + [""]


def by(ws: list[dict], f, prefix: str = "") -> list[tuple[str, list[dict]]]:
    keys = Counter(_key(w, f) for w in ws)
    rank = lambda k: (ORDER.index(k.split(" [")[0]) if k.split(" [")[0] in ORDER else 50, k)
    return [(prefix + k, [w for w in ws if _key(w, f) == k]) for k in sorted(keys, key=rank)]


def _tod(w, tz):
    t = datetime.fromtimestamp(w["t"] / 1000, timezone.utc).astimezone(tz)
    m = t.hour * 60 + t.minute
    return next((n for n, a, b in TOD if a <= m < b), "other")


def lines(v1rows: list[dict], fams: dict[str, list[dict]], names: dict[str, str], tz: ZoneInfo) -> list[str]:
    """v1rows: v1 main-lane rows; fams: v2 family -> main-lane rows. Rows carry side, t, price, context, r*, mfe_*,
    mae_*."""
    v2 = [w for ws in fams.values() for w in ws]
    allw = v1rows + v2
    rec = [w for w in allw if optx(w)]
    L = ["## 12. Options context at watch time (context only; measurement, not a filter)", "",
         "What could be known when the watch arrived: the latest MIAS options cycle at or before the watch, stored "
         "with the watch (`context.optx`, source **live snapshot**). Definitions: `mias/observer/optctx.py`. Flow "
         "leads; PCR direction and skew can only support or contradict it. Watches recorded before 2026-11-16 have "
         "no stored context and are shown as **not recorded** (never reconstructed). Do not tune any Early Watch "
         "rule or threshold from these tables.", "",
         f"- watches (main lane): {len(allw)} · with a stored context: {len(rec)} · sources: "
         + ", ".join(f"{k} {n}" for k, n in Counter(source(w) for w in allw).most_common()), ""]
    if rec:
        st = Counter(optx(w)["status"] for w in rec)
        ms = [optx(w).get("compute_ms") for w in rec]
        ages = [optx(w).get("age_s") for w in rec]
        md = lambda xs, n: "–" if _med(xs) is None else f"{_med(xs):.{n}f}"
        L += [f"- status: " + ", ".join(f"{k} {n}" for k, n in st.most_common()) +
              f" · snapshot age at watch s (median / max): {md(ages, 0)} / "
              f"{max([a for a in ages if a is not None], default=0):.0f} · context compute ms (median / max): "
              f"{md(ms, 2)} / {max([m for m in ms if m is not None], default=0):.1f}",
              "- group labels carry the source in brackets when it is not a live snapshot; sources are never pooled",
              ""]
    L += ["### 12a. By options context (relative to the watch direction)", ""]
    L += group_table([("V1 (control) · " + k, ws) for k, ws in by(v1rows, klass)] +
                     [("V2 all families · " + k, ws) for k, ws in by(v2, klass)])
    L += ["### 12b. By family", ""]
    L += group_table([g for fam, ws in fams.items() for g in by(ws, klass, names[fam] + " · ")])
    st = fams.get("structure", [])
    L += ["### 12c. Do V2-A Structure watches behave differently when options confirm vs conflict?", "",
          "Structure watches only. Same horizons; % correct = signed stock move > 0. Small samples: read as "
          "description, not evidence.", ""]
    L += group_table(by(st, klass, "Structure · "))
    L += ["### 12d. Time of day (all v2 families)", ""]
    L += group_table([g for n, _, _ in TOD for g in by([w for w in v2 if _tod(w, tz) == n], klass, n + " · ")])
    L += ["### 12e. Index (SPY / QQQ / IWM) vs single stocks (all v2 families)", ""]
    L += group_table(by([w for w in v2 if w["sym"] in INDEX], klass, "Index · ") +
                     by([w for w in v2 if w["sym"] not in INDEX], klass, "Single stock · "))
    L += ["Sample quality per symbol (watches with a stored context, v1 + v2):", "",
          "| Symbol | Watches | Usable | Insufficient | Stale | Median day option volume | Median top-contract share | "
          "Median rolling-15m contracts | Reasons |", "|---|---:|---:|---:|---:|---:|---:|---:|---|"]
    for sym in sorted({w["sym"] for w in rec}, key=lambda s: (s not in INDEX, s)):
        xs = [optx(w) for w in rec if w["sym"] == sym]
        rs = Counter(r.split(" (")[0] for x in xs for r in (x.get("reasons") or []))
        L.append(f"| {sym} | {len(xs)} | {sum(x['status'] == 'ok' for x in xs)} | "
                 f"{sum(x['status'] == 'insufficient' for x in xs)} | {sum(x['status'] == 'stale' for x in xs)} | "
                 f"{_f(_med([x.get('day_volume') for x in xs]), 0)} | {_f(_med([x.get('top_volume_share') for x in xs]))} | "
                 f"{_f(_med([(x.get('roll15') or {}).get('contracts') for x in xs]), 0)} | "
                 + (", ".join(f"{k} {n}" for k, n in rs.most_common(3)) or "–") + " |")
    L += ["", "### 12f. Single stocks: stock options + market (SPY / QQQ) options = overall", ""]
    L += group_table(by([w for w in v2 if w["sym"] not in INDEX], overall_of, "Overall · "))
    L += ["### 12g. PCR divergence (research, weak context only; never a reversal call)", ""]
    dv = [w for w in rec if (optx(w) or {}).get("divergence")]
    L += group_table([(f"{d.replace('POTENTIAL_', '').lower()} · {s} watch",
                       [w for w in dv if optx(w)["divergence"] == d and w["side"] == s])
                      for d in ("POTENTIAL_BULLISH_DIVERGENCE", "POTENTIAL_BEARISH_DIVERGENCE") for s in ("bull", "bear")])
    if not dv:
        L += ["- none recorded", ""]
    L += ["### 12h. Every watch with a stored context", "",
          "| Day | ET | Family | Symbol | Side | Price | Context | Recent flow | Session flow | PCR vol | PCR OI | "
          "PCR vol ex-0DTE | Roll15 now / prev | Roll30 now / prev | Slope15 | PCR dir | Skew | Age s | r15 % | r60 % |",
          "|---|---|---|---|---|---:|---|---|---|---:|---:|---:|---|---|---:|---|---|---:|---:|---:|"]
    hm = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(tz).strftime("%H:%M")
    n2 = lambda x: "–" if x is None else f"{x:.2f}"
    for w in sorted(rec, key=lambda w: (w["day"], w["t"], w["sym"]))[:400]:
        x = optx(w)
        r15, r30 = x.get("roll15") or {}, x.get("roll30") or {}
        L.append(f"| {w['day'][5:]} | {hm(w['t'])} | {w['family']} | {w['sym']} | {w['side']} | {w['price']:.2f} | "
                 f"{klass(w)} | {x.get('recent_dir') or '–'} | {x.get('session_dir') or '–'} | {n2(x.get('pcr_volume'))} | "
                 f"{n2(x.get('pcr_oi'))} | {n2(x.get('pcr_volume_ex0'))} | {n2(r15.get('cur'))} / {n2(r15.get('prev'))} | "
                 f"{n2(r30.get('cur'))} / {n2(r30.get('prev'))} | {_f(r15.get('slope'))} | {x.get('pcr_dir') or '–'} | "
                 f"{x.get('skew_dir') or '–'} | {_f(x.get('age_s'), 0)} | {_f(w.get('r15'))} | {_f(w.get('r60'))} |")
    if not rec:
        L.append("| – | | | | | | none yet | | | | | | | | | | | | | |")
    return L + [""]


CSV_KEYS = ["opt_source", "opt_status", "opt_class", "opt_relation", "opt_overall", "opt_view", "opt_market_view",
            "opt_flow_dir", "opt_recent_dir", "opt_session_dir", "opt_skew_dir", "opt_pcr_dir", "opt_pcr_basis",
            "opt_pcr_volume", "opt_pcr_oi", "opt_pcr_volume_0dte", "opt_pcr_volume_ex0", "opt_roll15", "opt_roll15_prev",
            "opt_roll15_slope", "opt_roll30", "opt_roll30_prev", "opt_divergence", "opt_age_s", "opt_fresh",
            "opt_snapshot_ts", "opt_compute_ms", "opt_reasons"]


def csv_values(w: dict) -> list:
    x = optx(w)
    if not x:
        return [NOT_RECORDED] + [None] * (len(CSV_KEYS) - 1)
    r15, r30 = x.get("roll15") or {}, x.get("roll30") or {}
    s = w["side"]
    return [x.get("source"), x.get("status"), x["class"][s], x["relation"][s], x["overall"][s], x.get("view"),
            x.get("market_view"), x.get("flow_dir"), x.get("recent_dir"), x.get("session_dir"), x.get("skew_dir"),
            x.get("pcr_dir"), x.get("pcr_basis"), x.get("pcr_volume"), x.get("pcr_oi"), x.get("pcr_volume_0dte"),
            x.get("pcr_volume_ex0"), r15.get("cur"), r15.get("prev"), r15.get("slope"), r30.get("cur"),
            r30.get("prev"), x.get("divergence"), x.get("age_s"), x.get("fresh"), x.get("snapshot_ts"),
            x.get("compute_ms"), "; ".join(x.get("reasons") or [])]
