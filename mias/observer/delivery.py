"""Detection vs delivery, short-horizon scoring and the fresh-setup table (report only, added 2026-11-12).

Stages (every event can be placed in one):
  1 RAW CANDIDATE        v1: a lane / symbol / side / leg on which the criteria held on 2 closes; v2: a candidate
  2 WATCH CREATED        a watch row
  3 TELEGRAM ELIGIBLE    a watch the delivery policy passed to the sender (= 4 + 5)
  4 TELEGRAM SENT
  5 TELEGRAM FAILED      the sender tried and Telegram / the network refused (reason recorded)
  6 TELEGRAM SUPPRESSED  a watch the delivery policy did not send, with an explicit reason
  7 TELEGRAM MUTED       (v2, from 2026-11-16) TELEGRAM POLICY CHECK: the family never sends alone
                         ('structure_telegram_muted'); intentional policy, never a failure / stale / duplicate.
                         It may still be listed in a VWAP / opening-range message sent for the same symbol / side.
  (NOT ATTEMPTED         replay rows / Telegram switched off: no delivery happened or was intended)

v1's 'hourly_cap' (and active_watch, cooldown, ...) are FROZEN DETECTOR reasons between stages 1 and 2: no watch
was created, so they are never Telegram suppressions. The v2 'hourly cap' Telegram suppression was removed on
2026-11-12; historical rows that carry it are kept and shown as such.

Short horizons: signed return at watch + N min (close of the last 1-minute bar ending at or before), and MFE / MAE
over the fixed window [watch, watch + N min] from 1-minute highs / lows, N in 5 / 10 / 15 / 30 / 60; empty if the
window runs past 16:00. Stock-price movement, not option P/L. Directionally correct = signed return > 0.
Measurement only: nothing here is an entry or exit rule.
"""
from __future__ import annotations

from datetime import datetime, timezone
from statistics import median
from zoneinfo import ZoneInfo

from .rules import ONE

HORIZONS = (5, 10, 15, 30, 60)
SENT, FAILED, SUPP, NA = "TELEGRAM SENT", "TELEGRAM FAILED", "TELEGRAM SUPPRESSED", "NOT ATTEMPTED"
MUTED = "TELEGRAM MUTED"
SHOWN = "listed in a combined VWAP / opening-range message"


def _close_ms(t: int, tz: ZoneInfo) -> int:
    return int(datetime.fromtimestamp(t / 1000, timezone.utc).astimezone(tz)
               .replace(hour=16, minute=0, second=0, microsecond=0).timestamp() * 1000)


def horizon_scores(bars, day: str, sym: str, side: str, t: int, price: float, tz: ZoneInfo) -> dict:
    sg = 1 if side == "bull" else -1
    cm = _close_ms(t, tz)
    out = {}
    for n in HORIZONS:
        end = t + n * ONE
        r = mfe = mae = None
        if end <= cm and price:
            c = bars.close_at(day, sym, end)
            r = None if c is None else 100 * sg * (c / price - 1)
            path = bars.path(day, sym, t, end)
            if path:
                mfe = max(100 * sg * ((x[2] if sg > 0 else x[3]) / price - 1) for x in path)
                mae = min(100 * sg * ((x[3] if sg > 0 else x[2]) / price - 1) for x in path)
        out[f"r{n}"], out[f"mfe_{n}"], out[f"mae_{n}"] = r, mfe, mae
    return out


def v1_stage(w: dict) -> tuple[str, str | None]:
    err = w.get("telegram_error") or ""
    if w.get("telegram_sent"):
        return SENT, None
    if not err and w.get("mode") != "live":
        return NA, "replay / Telegram off"
    if w.get("lane") != "main" or w.get("opening_shadow_only"):
        return SUPP, "outside Telegram window (opening lane, research only)"
    if err.startswith("not sent"):
        return SUPP, ("stale (" + err[len("not sent: "):] + ")") if "old when found" in err else "other: " + err
    if err:
        return FAILED, err
    return SUPP, "other: no delivery record (Telegram off or stopped before sending)"


def v2_stage(w: dict) -> tuple[str, str | None]:
    err = w.get("telegram_error") or ""
    if w.get("telegram_sent"):
        return SENT, None
    if not err and w.get("mode") != "live":
        return NA, "replay / Telegram off"
    if err.startswith("muted: "):
        reason = err[len("muted: "):].split(";")[0]
        return MUTED, f"{reason} · {SHOWN}" if "shown in combined" in err else reason
    if err.startswith("merged into"):
        return SUPP, "combined: a same symbol / direction v2 message went out ≤ 5 min earlier"
    if err.startswith("not sent: lane"):
        return SUPP, f"outside Telegram window ({err[len('not sent: '):]})"
    if err == "not sent: found late":
        return SUPP, "stale (found later than telegram_max_age_seconds)"
    if err == "not sent: setup already sent":
        return SUPP, "already sent (same family / symbol / side / setup)"
    if err == "not sent: hourly cap":
        return SUPP, "hourly_cap (historical; cap removed 2026-11-12)"
    if err.startswith("not sent"):
        return SUPP, "other: " + err
    if err:
        return FAILED, err
    return SUPP, "other: no delivery record (Telegram off or stopped before sending)"


# ---- formatting -------------------------------------------------------------------------------------------

def _f(x, n=3):
    return "–" if x is None else f"{x:+.{n}f}"


def _med(xs):
    xs = [x for x in xs if x is not None]
    return median(xs) if xs else None


def _hm(ms, tz):
    return "–" if not ms else datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(tz).strftime("%H:%M")


def funnel_lines(groups: list[tuple[str, int | None, list[dict]]]) -> list[str]:
    """groups: (name, raw candidate count, watches with 'stage' / 'stage_reason')."""
    L = ["| Family | RAW CANDIDATE | WATCH CREATED | TELEGRAM ELIGIBLE | TELEGRAM SENT | TELEGRAM FAILED | "
         "TELEGRAM SUPPRESSED | TELEGRAM MUTED (policy) | not attempted (replay / off) |",
         "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    reasons = []
    for name, raw, ws in groups:
        c = {s: sum(1 for w in ws if w["stage"] == s) for s in (SENT, FAILED, SUPP, MUTED, NA)}
        L.append(f"| {name} | {'–' if raw is None else raw} | {len(ws)} | {c[SENT] + c[FAILED]} | {c[SENT]} | "
                 f"{c[FAILED]} | {c[SUPP]} | {c[MUTED]} | {c[NA]} |")
        rc: dict[str, int] = {}
        for w in ws:
            if w["stage"] in (SUPP, FAILED, MUTED):
                k = f"{w['stage'].split()[-1].lower()}: {w['stage_reason']}"
                rc[k] = rc.get(k, 0) + 1
        if rc:
            reasons.append(f"- {name}: " + " · ".join(f"{k} {n}" for k, n in sorted(rc.items(), key=lambda x: -x[1])))
    return L + ([""] + reasons if reasons else [])


def missed_lines(rows: list[dict], tz: ZoneInfo, limit: int = 400) -> list[str]:
    """rows: watches with sym, side, family, t, price, stage, stage_reason, r5..r60."""
    miss = [w for w in rows if w["stage"] in (SUPP, FAILED, MUTED)]
    na = sum(1 for w in rows if w["stage"] == NA)
    if not miss:
        return ["- none" + (f" ({na} replay / Telegram-off watches were not delivery attempts)" if na else ""), ""]
    L = ["| Day | Watch ET | Symbol | Direction | Family | Lane | Watch price | Stage | Reason | r5 % | r10 % | r15 % | "
         "r30 % | r60 % |", "|---|---|---|---|---|---|---:|---|---|---:|---:|---:|---:|---:|"]
    for w in sorted(miss, key=lambda w: (w["day"], w["t"], w["sym"]))[:limit]:
        L.append(f"| {w['day'][5:]} | {_hm(w['t'], tz)} | {w['sym']} | {w['side']} | {w['family']} | {w['lane']} | "
                 f"{w['price']:.2f} | {w['stage'].split()[-1]} | {w['stage_reason']} | "
                 + " | ".join(_f(w.get(f"r{n}")) for n in HORIZONS) + " |")
    if len(miss) > limit:
        L.append(f"| … {len(miss) - limit} more in the CSV | | | | | | | | | | | | | |")
    return L + [""]


def horizon_lines(groups: list[tuple[str, list[dict]]]) -> list[str]:
    L = ["Directionally correct = signed stock move > 0 at watch + N min. MFE / MAE over the fixed window to + N min "
         "(medians). Watches whose window runs past 16:00 are left out of that horizon.", "",
         "| Family | Horizon | n | % correct | median signed % | median MFE % | median MAE % |",
         "|---|---|---:|---:|---:|---:|---:|"]
    for name, ws in groups:
        for n in HORIZONS:
            xs = [w[f"r{n}"] for w in ws if w.get(f"r{n}") is not None]
            pct = f"{100 * sum(x > 0 for x in xs) / len(xs):.0f}%" if xs else "–"
            L.append(f"| {name} | {n}m | {len(xs)} | {pct} | {_f(_med(xs))} | "
                     f"{_f(_med([w.get(f'mfe_{n}') for w in ws]))} | {_f(_med([w.get(f'mae_{n}') for w in ws]))} |")
    return L + [""]


def fresh_rows(db, bars, tz: ZoneInfo, day: str | None = None) -> list[dict]:
    q, args = ("day = ?", (day,)) if day else ("1=1", ())
    cur = db.db.execute(f"SELECT * FROM fresh_while_open WHERE {q} ORDER BY cand_at, sym", args)
    cols = [d[0] for d in cur.description]
    out = []
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        d.update(horizon_scores(bars, d["day"], d["sym"], d["side"], d["cand_at"], d["cand_price"], tz))
        out.append(d)
    return out


def fresh_lines(rows: list[dict], tz: ZoneInfo, limit: int = 300) -> list[str]:
    if not rows:
        return ["- none", ""]
    L = ["Measurement only: the frozen rules did not (and still do not) allow a second watch. "
         "Outcomes are from the NEW candidate's time and price.", "",
         "| Day | New setup ET | Symbol | Side | Family / setup | Price | Old watch (created ET) | Old watch minutes left | "
         "Would otherwise qualify | r5 % | r10 % | r15 % | r30 % | r60 % | MFE15 % | MAE15 % |",
         "|---|---|---|---|---|---:|---|---:|---|---:|---:|---:|---:|---:|---:|---:|"]
    for d in rows[:limit]:
        q = {1: "yes", 0: "no"}.get(d["would_qualify"], "unknown")
        L.append(f"| {d['day'][5:]} | {_hm(d['cand_at'], tz)} | {d['sym']} | {d['side']} | {d['family']} / "
                 f"{d['setup']} | {d['cand_price']:.2f} | {d['old_leg_id']} ({_hm(d['old_created_at'], tz)}) | "
                 f"{d['remaining_min']:.0f} | {q} ({d['qualify_detail']}) | "
                 + " | ".join(_f(d.get(f"r{n}")) for n in HORIZONS)
                 + f" | {_f(d.get('mfe_15'))} | {_f(d.get('mae_15'))} |")
    if len(rows) > limit:
        L.append(f"- … {len(rows) - limit} more rows")
    yes = [d for d in rows if d["would_qualify"] == 1]
    L += ["", f"- {len(rows)} fresh setups while a same-direction watch was open · would otherwise qualify {len(yes)}"
              f" · r15 median (qualifying) {_f(_med([d.get('r15') for d in yes]))} % · r60 median (qualifying) "
              f"{_f(_med([d.get('r60') for d in yes]))} %", ""]
    return L


# ---- v2 family Telegram mute (2026-11-16; report only) -----------------------------------------------------

COMBOS = ("structure only", "structure + vwap", "structure + or", "structure + vwap + or", "vwap only", "or only",
          "vwap + or")


def combo(fams) -> str:
    return " + ".join(f for f in ("structure", "vwap", "or") if f in fams) + ("" if len(set(fams)) > 1 else " only")


def _open_at(rows: list[dict], sym: str, side: str, t: int) -> list[dict]:
    return [o for o in rows if o["sym"] == sym and o["side"] == side and o["watch_at"] <= t
            and (o.get("end_at") is None or o["end_at"] > t)]


def policy_replay(rows: list[dict], muted=(), combine_s: int = 300, max_age_s: float = 180) -> list[dict]:
    """Re-run the pure v2 Telegram policy (mias.observer.v2.V2Telegram) over stored live watches in arrival order.
    Returns one dict per message: {'w', 'fams', 'listed'} (listed = open watches of muted families the message
    would show). An ESTIMATE of what a policy would have sent; recorded delivery is never rewritten."""
    from .v2 import V2Telegram
    pol = V2Telegram(combine_s=combine_s, max_age_s=max_age_s, muted=muted)
    live = sorted((w for w in rows if w.get("mode") == "live"),
                  key=lambda w: (w.get("received_at") or w["watch_at"] / 1000, w["watch_at"], w["watch_id"]))
    out = []
    for w in live:
        now = int(1000 * (w.get("received_at") or w["watch_at"] / 1000))
        verdict, _ = pol.decide(w, now)
        if verdict == "send":
            op = _open_at(rows, w["sym"], w["side"], w["watch_at"])
            out.append({"w": w, "fams": {o["family"] for o in op} | {w["family"]},
                        "listed": [o["watch_id"] for o in op if o["family"] in pol.muted]})
    return out


def mute_lines(rows: list[dict], muted, names: dict[str, str]) -> list[str]:
    """Section 8a: historical actual delivery (as recorded) vs the current delivery policy (replay estimate)."""
    from . import optreport
    from collections import Counter
    if not muted:
        return []
    fam = sorted(muted)[0] if len(muted) == 1 else None
    title = f"{names.get(fam, fam)} Telegram-only mute" if fam else "v2 family Telegram-only mute"
    ws = [w for w in rows if w["family"] in muted and w["lane"] == "main"]
    L = [f"### 8a. {title}", "",
         f"Delivery policy since 2026-11-16 (docs/observer/OPERATIONAL_CHANGES.md): {', '.join(sorted(muted))} "
         "watches are still detected, stored, given options context, tracked and scored here exactly like every "
         "other family; they never send a Telegram message of their own (`<family>_telegram_muted`, stage TELEGRAM "
         "MUTED: intentional, not failed / stale / duplicate). A VWAP / opening-range message for the same symbol / "
         "side still lists an open muted watch. Rows recorded before the change keep their actual delivery.", "",
         "**Historical actual delivery (as recorded)**, main lane:", ""]
    st = Counter(w["stage"] for w in ws)
    shown = sum(1 for w in ws if w["stage"] == MUTED and SHOWN in (w["stage_reason"] or ""))
    L += [f"- watches created {len(ws)} · sent {st[SENT]} · failed {st[FAILED]} · suppressed (other reasons) "
          f"{st[SUPP]} · muted {st[MUTED]} (of which {shown} listed in a combined VWAP / opening-range message, "
          f"{st[MUTED] - shown} muted alone) · not attempted {st[NA]}", ""]
    rec = [w for w in rows if w.get("telegram_sent")]
    old, new = policy_replay(rows, ()), policy_replay(rows, muted)
    rc = Counter(combo({o["family"] for o in _open_at(rows, w["sym"], w["side"], w["watch_at"])} | {w["family"]})
                 for w in rec)
    oc, nc = Counter(combo(m["fams"]) for m in old), Counter(combo(m["fams"]) for m in new)
    L += ["**Current delivery policy (replay of the stored watches, estimate)**: message counts by the families a "
          "message lists. 'Recorded' = what was actually sent; 'no mute' = the policy without the mute (sanity check "
          "against recorded); 'current' = with the mute.", "",
          "| Message lists | Recorded | Replay: no mute | Replay: current policy |", "|---|---:|---:|---:|"]
    for k in COMBOS:
        if rc[k] or oc[k] or nc[k]:
            L.append(f"| {k} | {rc[k]} | {oc[k]} | {nc[k]} |")
    L.append(f"| **total** | **{len(rec)}** | **{len(old)}** | **{len(new)}** |")
    led = sum(1 for m in old if m["w"]["family"] in muted)
    newids = {m["w"]["watch_id"] for m in new} - {m["w"]["watch_id"] for m in old}
    L += ["", f"- messages led by a muted watch removed: {led} · VWAP / opening-range watches that were merged into "
              f"such a message and now send their own: {len(newids)} · net change {len(new) - len(old):+d}", ""]
    listed = {i for m in new for i in m["listed"]}
    alone = [w for w in ws if w["watch_id"] not in listed]
    shownw = [w for w in ws if w["watch_id"] in listed]
    L += ["Outcomes of the muted family under the current policy (all metrics as in sections 6, 10 and 12):", ""]
    L += horizon_lines([("muted alone (no message)", alone), ("listed in a combined message", shownw)])
    L += optreport.group_table([(f"muted alone · {k}", g) for k, g in optreport.by(alone, optreport.klass)] +
                               [(f"listed in combined · {k}", g) for k, g in optreport.by(shownw, optreport.klass)])
    return L
