"""Detection vs delivery, short-horizon scoring and the fresh-setup table (report only, added 2026-11-12).

Stages (every event can be placed in one):
  1 RAW CANDIDATE        v1: a lane / symbol / side / leg on which the criteria held on 2 closes; v2: a candidate
  2 WATCH CREATED        a watch row
  3 TELEGRAM ELIGIBLE    a watch the delivery policy passed to the sender (= 4 + 5)
  4 TELEGRAM SENT
  5 TELEGRAM FAILED      the sender tried and Telegram / the network refused (reason recorded)
  6 TELEGRAM SUPPRESSED  a watch the delivery policy did not send, with an explicit reason
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
         "TELEGRAM SUPPRESSED | not attempted (replay / off) |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    reasons = []
    for name, raw, ws in groups:
        c = {s: sum(1 for w in ws if w["stage"] == s) for s in (SENT, FAILED, SUPP, NA)}
        L.append(f"| {name} | {'–' if raw is None else raw} | {len(ws)} | {c[SENT] + c[FAILED]} | {c[SENT]} | "
                 f"{c[FAILED]} | {c[SUPP]} | {c[NA]} |")
        rc: dict[str, int] = {}
        for w in ws:
            if w["stage"] in (SUPP, FAILED):
                k = f"{w['stage'].split()[-1].lower()}: {w['stage_reason']}"
                rc[k] = rc.get(k, 0) + 1
        if rc:
            reasons.append(f"- {name}: " + " · ".join(f"{k} {n}" for k, n in sorted(rc.items(), key=lambda x: -x[1])))
    return L + ([""] + reasons if reasons else [])


def missed_lines(rows: list[dict], tz: ZoneInfo, limit: int = 400) -> list[str]:
    """rows: watches with sym, side, family, t, price, stage, stage_reason, r5..r60."""
    miss = [w for w in rows if w["stage"] in (SUPP, FAILED)]
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
