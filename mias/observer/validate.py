"""Phase A: the day's WebSocket AM bars against REST 1-minute aggregates (written every evening at 20:10 ET to
data/observer/validation/<day>.md, or on demand: python -m mias.observer validate --date YYYY-MM-DD).

Acceptance (pre-registered, docs/observer/PREREGISTRATION.md section A):
  regular session, per day: median arrival latency <= 5 s and p99 <= 30 s after bar end; stream bars missing vs REST
  <= 1% (and every gap recovered from REST before evaluation); close equal to REST (within $0.01) on >= 99% of
  matched bars; no unrecovered gap (gap_unfilled = 0).
"""
from __future__ import annotations

from datetime import date, datetime, time
from statistics import median
from zoneinfo import ZoneInfo

from .rules import ONE


def _pct(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * (len(xs) - 1) + 0.5))]


def _ms(day: date, hm: time, tz: ZoneInfo) -> int:
    return int(datetime.combine(day, hm, tz).timestamp() * 1000)


async def validate(client, db, symbols: list[str], day: date, tz: ZoneInfo) -> str:
    d0, d1 = _ms(day, time(0, 0), tz), _ms(day, time(23, 59), tz)
    o, c = _ms(day, time(9, 30), tz), _ms(day, time(16, 0), tz)
    rows = db.db.execute("SELECT sym, s, e, o, h, l, c, v, vw, a, received_at, status FROM ws_bars "
                         "WHERE s >= ? AND s < ?", (d0, d1)).fetchall()
    lines = [f"# REST vs WebSocket — {day}", "",
             "Regular session = bars starting 09:30–15:59 ET. Latency = receive time − bar end (e).", ""]
    if not rows:
        lines.append("No WebSocket bars recorded for this day.")
        return "\n".join(lines)
    totals = {"lat": [], "missing": 0, "rest": 0, "extra": 0, "matched": 0, "close_eq": 0, "dup": 0, "old": 0,
              "revised": 0}
    table = ["| Symbol | REST bars | WS bars | missing | extra | dup | revised | out-of-order | latency p50 / p90 / max s "
             "| close = REST | max |Δ| close | vol Δ median | bar VWAP Δ bps median | session VWAP (a) Δ bps: from 09:30 / from 04:00 |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for sym in symbols:
        raw = await client.aggregates(sym, 1, "minute", day, day)
        rest = {int(r["t"]): r for r in raw}
        reg = {t: r for t, r in rest.items() if o <= t < c}
        ws = [r for r in rows if r[0] == sym]
        ok = {r[1]: r for r in ws if r[11] == "ok" and o <= r[1] < c}
        status = {k: sum(1 for r in ws if r[11] == k and o <= r[1] < c) for k in ("dup", "old", "revised")}
        lat = [r[10] * 1000 - r[2] for r in ok.values()]
        missing = [t for t in reg if t not in ok]
        extra = [t for t in ok if t not in reg]
        matched = [t for t in ok if t in reg]
        dclose = [abs(ok[t][6] - reg[t]["c"]) for t in matched]
        dvol = [(ok[t][7] - reg[t]["v"]) / reg[t]["v"] * 100 for t in matched if reg[t].get("v")]
        dvw = [abs(ok[t][8] - reg[t]["vw"]) / reg[t]["vw"] * 1e4 for t in matched
               if reg[t].get("vw") and ok[t][8] is not None]
        sess_a, sess_b = [], []
        pv_reg = v_reg = pv_all = v_all = 0.0
        for t in sorted(rest):
            r = rest[t]
            w = r.get("vw") or (r["h"] + r["l"] + r["c"]) / 3
            pv_all += w * r["v"]
            v_all += r["v"]
            if o <= t < c:
                pv_reg += w * r["v"]
                v_reg += r["v"]
            if t in ok and ok[t][9] and o <= t < c and v_reg and v_all:
                sess_a.append(abs(ok[t][9] - pv_reg / v_reg) / ok[t][9] * 1e4)
                sess_b.append(abs(ok[t][9] - pv_all / v_all) / ok[t][9] * 1e4)
        eq = sum(1 for x in dclose if x <= 0.01 + 1e-9)
        totals["lat"] += lat
        totals["missing"] += len(missing)
        totals["rest"] += len(reg)
        totals["extra"] += len(extra)
        totals["matched"] += len(matched)
        totals["close_eq"] += eq
        for k in ("dup", "old", "revised"):
            totals[k] += status[k]
        f = lambda x, n=1: "–" if x is None else f"{x:.{n}f}"
        table.append(f"| {sym} | {len(reg)} | {len(ok)} | {len(missing)} | {len(extra)} | {status['dup']} | "
                     f"{status['revised']} | {status['old']} | {f(_pct(lat, .5) and _pct(lat, .5) / 1000)} / "
                     f"{f(_pct(lat, .9) and _pct(lat, .9) / 1000)} / {f(max(lat) / 1000 if lat else None)} | "
                     f"{eq}/{len(matched)} | {f(max(dclose) if dclose else None, 3)} | "
                     f"{f(median(dvol) if dvol else None, 2)}% | {f(median(dvw) if dvw else None, 2)} | "
                     f"{f(median(sess_a) if sess_a else None, 2)} / {f(median(sess_b) if sess_b else None, 2)} |")
    lat = totals["lat"]
    feed = db.db.execute("SELECT kind, COUNT(*) FROM feed_events WHERE ts >= ? AND ts < ? GROUP BY kind",
                         (d0 / 1000, d1 / 1000)).fetchall()
    fd = dict(feed)
    p50, p99 = _pct(lat, .5), _pct(lat, .99)
    miss_pct = 100 * totals["missing"] / totals["rest"] if totals["rest"] else None
    close_pct = 100 * totals["close_eq"] / totals["matched"] if totals["matched"] else None
    checks = [("median latency <= 5 s", p50 is not None and p50 <= 5000),
              ("p99 latency <= 30 s", p99 is not None and p99 <= 30000),
              ("missing <= 1%", miss_pct is not None and miss_pct <= 1),
              ("close = REST on >= 99%", close_pct is not None and close_pct >= 99),
              ("no unrecovered gap", fd.get("gap_unfilled", 0) == 0)]
    lines += table + ["", "## Totals", "",
                      f"- latency p50 {p50 / 1000 if p50 is not None else '–'} s · p99 "
                      f"{p99 / 1000 if p99 is not None else '–'} s · max {max(lat) / 1000 if lat else '–'} s",
                      f"- missing {totals['missing']} of {totals['rest']} REST bars"
                      f" ({miss_pct:.2f}%)" if miss_pct is not None else "- missing –",
                      f"- extra {totals['extra']} · dup {totals['dup']} · revised {totals['revised']} · "
                      f"out-of-order {totals['old']}",
                      f"- close = REST on {close_pct:.2f}% of matched bars" if close_pct is not None else "- close –",
                      f"- feed events: {dict(sorted(fd.items()))}", "", "## Acceptance", ""]
    lines += [f"- {'PASS' if ok_ else 'FAIL'} — {name}" for name, ok_ in checks]
    lines.append(f"\n**{'ACCEPTABLE' if all(x for _, x in checks) else 'NOT ACCEPTABLE'}**")
    return "\n".join(lines)
