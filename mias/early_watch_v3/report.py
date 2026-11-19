"""V3 daily research report, V2-vs-V3 comparison and latency summary (Markdown). Report only: nothing here changes
detection, delivery or storage. Rates are shown only when n >= MIN_N; below that, raw counts labelled
"sample too small". The first review (two sessions) is counts and direction only; no tuning from it."""
from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from zoneinfo import ZoneInfo

from .outcomes import outcomes
from .store import HORIZONS, V3DB

MIN_N = 30
BUCKETS = (("09:30–10:00", "09:30", "10:00"), ("10:00–11:00", "10:00", "11:00"), ("11:00–12:00", "11:00", "12:00"),
           ("12:00–13:00", "12:00", "13:00"), ("13:00–14:00", "13:00", "14:00"), ("14:00–15:00", "14:00", "15:00"),
           ("15:00–16:00", "15:00", "16:01"))


def rate(k: int, n: int) -> str:
    if n >= MIN_N:
        return f"{100 * k / n:.0f}% ({k}/{n})"
    return f"{k}/{n} (sample too small)" if n else "–"


def _hhmm(ms: int, tz: ZoneInfo) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(tz).strftime("%H:%M")


def fill_outcomes(db: V3DB, day: str, t_close_ms: int) -> int:
    n = 0
    cache: dict[str, list] = {}
    for w in db.watches("day = ?", (day,)):
        bars = cache.setdefault(w["sym"], db.bars(day, w["sym"]))
        db.set_outcomes(w["watch_id"], outcomes(w, bars, t_close_ms))
        n += 1
    return n


def _group_rows(ws: list[dict], key, label: str) -> list[str]:
    groups: dict = defaultdict(list)
    for w in ws:
        groups[key(w)].append(w)
    out = [f"\n**By {label}**\n", "| group | n | " + " | ".join(f"{h}m right" for h in HORIZONS) +
           " | invalidated | expired |", "|---|---|" + "---|" * (len(HORIZONS) + 2)]
    for g in sorted(groups, key=lambda x: str(x)):
        rows = groups[g]
        cells = []
        for h in HORIZONS:
            xs = [r[f"out_{h}"] for r in rows if r.get(f"out_{h}") is not None]
            cells.append(rate(sum(x > 0 for x in xs), len(xs)))
        inv = sum(r["state"] == "INVALIDATED" for r in rows)
        exp = sum(r["state"] == "EXPIRED" for r in rows)
        out.append(f"| {g} | {len(rows)} | " + " | ".join(cells) + f" | {rate(inv, len(rows))} | "
                   f"{rate(exp, len(rows))} |")
    return out


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return f"{mean(xs):+.3f}" if xs else "–"


def build(db: V3DB, tz: ZoneInfo, day: str, v2_db: Path | None = None, out_dir: Path | None = None,
          title: str | None = None) -> str:
    ws = db.watches("day = ?", (day,))
    L = [f"# {title or f'V3 EARLY WATCH — {day}'}", "",
         "Research only. Detection = V2 clone; all three families sent to the V3 chat. Rates only at n >= "
         f"{MIN_N}; smaller groups are raw counts (sample too small). First review: counts and direction only.", ""]
    sent = [w for w in ws if w["telegram_sent"]]
    L += ["## Totals", f"- Watches: **{len(ws)}** (main lane {sum(w['lane'] == 'main' for w in ws)}, opening "
          f"{sum(w['lane'] == 'opening' for w in ws)}, late {sum(w['lane'] == 'late' for w in ws)})",
          f"- Telegram messages sent: **{len(sent)}** (merged into a combined message "
          f"{sum((w['v3_verdict'] or '') == 'merged' for w in ws)}, failed "
          f"{sum(w['v3_verdict'] == 'send' and not w['telegram_sent'] and not (w['telegram_error'] or '').startswith('not sent') for w in ws)}, "
          f"policy 'send' but not sent (switched off / replay) "
          f"{sum(w['v3_verdict'] == 'send' and (w['telegram_error'] or '').startswith('not sent') for w in ws)})"]
    fam = Counter(w["family"] for w in ws)
    fam_sent = Counter(w["family"] for w in sent)
    L.append("- By family: " + " · ".join(f"{f} {fam.get(f, 0)} (sent {fam_sent.get(f, 0)})"
                                          for f in ("structure", "vwap", "or")))
    side = Counter(w["side"] for w in ws)
    L.append(f"- Bull {side.get('bull', 0)} · Bear {side.get('bear', 0)}")
    tb = []
    for name, a, b in BUCKETS:
        tb.append(f"{name} {sum(a <= _hhmm(w['watch_at'], tz) < b for w in ws)}")
    L.append("- Time of day: " + " · ".join(tb))
    L.append(f"- V2 policy would have sent {sum(bool(w['v2_would_send']) for w in ws)} of these Watches on their own "
             "(V2-sent vs V2-muted grouping below)")

    L += ["", "## Outcomes (all Watches)", "| window | n | right (signed move > 0) | mean signed move % |",
          "|---|---|---|---|"]
    for h in HORIZONS:
        xs = [w[f"out_{h}"] for w in ws if w.get(f"out_{h}") is not None]
        L.append(f"| {h}m | {len(xs)} | {rate(sum(x > 0 for x in xs), len(xs))} | {_mean(xs)} |")
    L.append(f"\nMFE mean {_mean([w['mfe_pct'] for w in ws])}% · MAE mean {_mean([w['mae_pct'] for w in ws])}% "
             f"(60 min) · invalidated {rate(sum(w['state'] == 'INVALIDATED' for w in ws), len(ws))} · expired "
             f"{rate(sum(w['state'] == 'EXPIRED' for w in ws), len(ws))} · upgraded "
             f"{sum(w['state'] == 'UPGRADED' for w in ws)}")
    L += ["", "## Presentation flags (display only)"]
    fam_n = {}
    for w in ws:
        if w.get("invalidation_at_entry") is None:
            at = w.get("inval_dist_atr") is not None and abs(w["inval_dist_atr"]) < 0.05
        else:
            at = bool(w["invalidation_at_entry"])
        fam_n[w["family"]] = fam_n.get(w["family"], 0) + at
    L.append("- Invalidation at entry (< 0.05 ATR): " + " · ".join(
        f"{f} {fam_n.get(f, 0)}" for f in ("structure", "vwap", "or")) + f" · total {sum(fam_n.values())}")
    mm = [w for w in ws if w.get("market_mismatch")]
    L.append(f"- Market mismatch computed {len(mm)} · shown {sum(bool(w.get('mismatch_shown')) for w in mm)} "
             "(|SPY| >= 0.50%)")
    L += ["", "## Groups"]
    groupings = [("family", lambda w: w["family"]), ("Supports count", lambda w: w["n_support"]),
                 ("Against count", lambda w: w["n_against"]), ("Neutral count", lambda w: w["n_neutral"]),
                 ("low conviction", lambda w: bool(w["low_conviction"])),
                 ("late session", lambda w: bool(w["late_session"])),
                 ("tight invalidation", lambda w: bool(w["tight"])),
                 ("options Overall", lambda w: w["options_overall"] or "n/a"),
                 ("index vs single stock", lambda w: "index" if w["is_index"] else "single stock"),
                 ("V2 policy", lambda w: "V2 would send" if w["v2_would_send"] else "V2 muted / not sent")]
    for label, key in groupings:
        L += _group_rows(ws, key, label)
    L += ["", "## V2 vs V3"] + compare(db, day, v2_db, tz)
    L += ["", "## Latency"] + latency(ws)
    text = "\n".join(L) + "\n"
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "v3_report.md").write_text(text)
    return text


def compare(db: V3DB, day: str, v2_db: Path | None, tz: ZoneInfo) -> list[str]:
    """Detection should match V2 exactly; Telegram counts differ by design (V3 sends all families)."""
    if not v2_db or not Path(v2_db).exists():
        return ["- V2 database not available"]
    v2 = sqlite3.connect(f"file:{v2_db}?mode=ro", uri=True)
    try:
        c2 = v2.execute("SELECT cand_id, candidate_at, side, status FROM v2_candidate_log WHERE day=?",
                        (day,)).fetchall()
        w2 = v2.execute("SELECT watch_id, family, sym, side, leg_id, watch_at, telegram_sent FROM v2_watches "
                        "WHERE day=?", (day,)).fetchall()
    finally:
        v2.close()
    c3 = db.db.execute("SELECT cand_id, candidate_at, side, status FROM v3_candidate_log WHERE day=?",
                       (day,)).fetchall()
    w3 = db.db.execute("SELECT watch_id, family, sym, side, leg_id, watch_at, telegram_sent FROM v3_watches "
                       "WHERE day=?", (day,)).fetchall()
    m2 = {r[0]: r for r in w2}
    m3 = {r[0]: r for r in w3}
    both = set(m2) & set(m3)
    ts_diff = [(k, m2[k][5], m3[k][5]) for k in sorted(both) if m2[k][5] != m3[k][5]]
    legs2 = {(r[1], r[2], r[4]): r[3] for r in w2}
    legs3 = {(r[1], r[2], r[4]): r[3] for r in w3}
    dir_diff = [k for k in set(legs2) & set(legs3) if legs2[k] != legs3[k]]
    only2, only3 = sorted(set(m2) - set(m3)), sorted(set(m3) - set(m2))
    cand_same = sorted(c2) == sorted(c3)
    L = [f"- Candidates: V2 {len(c2)} · V3 {len(c3)} · identical (id, time, side, status): "
         f"**{'yes' if cand_same else 'NO'}**",
         f"- Watches: V2 {len(w2)} · V3 {len(w3)} · same ids {len(both)} · only V2 {len(only2)} · only V3 {len(only3)}",
         f"- Watch timestamp differences: {len(ts_diff)} · direction differences: {len(dir_diff)}",
         f"- Expected: detection matches → **{'MATCH' if not (ts_diff or dir_diff or only2 or only3) and cand_same else 'DIFFERENCE (see below)'}**",
         f"- Telegram messages: V2 sent {sum(r[6] for r in w2)} · V3 sent {sum(r[6] for r in w3)} "
         "(differs by design: V3 sends all three families)"]
    for k in (only2[:10] + only3[:10]):
        L.append(f"  - {'only V2' if k in m2 else 'only V3'}: {k}")
    for k, a, b in ts_diff[:10]:
        L.append(f"  - {k}: V2 {_hhmm(a, tz)} vs V3 {_hhmm(b, tz)}")
    return L


def latency(ws: list[dict]) -> list[str]:
    pairs = {"bar received → Watch created": ("bar_received_at", "created_at"),
             "Watch created → Telegram queued": ("created_at", "telegram_queued_at"),
             "Telegram queued → sent": ("telegram_queued_at", "telegram_sent_at"),
             "bar received → Telegram sent": ("bar_received_at", "telegram_sent_at")}
    L = ["| step | n | median s | max s |", "|---|---|---|---|"]
    for name, (a, b) in pairs.items():
        xs = [w[b] - w[a] for w in ws if w.get(a) is not None and w.get(b) is not None]
        L.append(f"| {name} | {len(xs)} | {median(xs):.2f} | {max(xs):.2f} |" if xs else f"| {name} | 0 | – | – |")
    return L
