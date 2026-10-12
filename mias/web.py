"""Tiny HTTP server: /healthz, /status and /screen.json (JSON), / and /screen (HTML). No dependencies."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from html import escape

from .formatting import label, money, num
from .notify import strip_html as strip_html_safe

log = logging.getLogger(__name__)
REFRESH_GAP_S = 300

CSS = """
:root{--bg:#fff;--fg:#1a1a1a;--mut:#666;--line:#ddd;--acc:#0b63c4;--good:#1a7f37;--bad:#c62828;--hi:#f3f6fa}
@media (prefers-color-scheme:dark){:root{--bg:#111;--fg:#eee;--mut:#999;--line:#333;--acc:#6aa9ff;
--good:#4cc36b;--bad:#ff6b6b;--hi:#1b2230}}
body{background:var(--bg);color:var(--fg);font:14px system-ui,sans-serif;margin:16px}
a{color:var(--acc)}nav{margin-bottom:12px}nav a{margin-right:14px}nav a.on{font-weight:700;text-decoration:none}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid var(--line);padding:4px 6px;text-align:left;white-space:nowrap}
th{cursor:pointer;user-select:none}td.n,th.n{text-align:right}tr.watch td{background:var(--hi)}
.wrap{overflow-x:auto}small,.mut{color:var(--mut)}pre{white-space:pre-wrap;margin:2px 0 8px}
.good{color:var(--good)}.bad{color:var(--bad)}.note{padding:8px 10px;border:1px solid var(--line);border-radius:6px;margin:8px 0}
"""

SORT_JS = """<script>
document.querySelectorAll('table.sort th').forEach((th,i)=>th.addEventListener('click',()=>{
const tb=th.closest('table').tBodies[0],rows=[...tb.rows],asc=th.dataset.asc!=='1';th.dataset.asc=asc?'1':'0';
const v=r=>{const t=r.cells[i].innerText.replace(/[$,%★+]/g,'').trim();const n=parseFloat(t);return isNaN(n)?t:n};
rows.sort((a,b)=>{const x=v(a),y=v(b);return (x>y?1:x<y?-1:0)*(asc?1:-1)});rows.forEach(r=>tb.appendChild(r))}));
</script>"""


def page(title: str, active: str, body: str, refresh: int | None = 60) -> str:
    nav = "".join(f'<a href="{href}" class="{"on" if key == active else ""}">{name}</a>'
                  for key, href, name in (("overview", "/", "Overview"), ("screen", "/screen", "Options screen")))
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return (f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1">{meta}<title>{escape(title)}</title>'
            f"<style>{CSS}</style></head><body><nav>{nav}</nav>{body}{SORT_JS}</body></html>")


class WebServer:
    def __init__(self, service):
        self.svc = service
        self._server: asyncio.base_events.Server | None = None

    async def start(self, host: str, port: int) -> None:
        self._server = await asyncio.start_server(self._handle, host, port)
        log.info("web server on http://%s:%d", host, port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            while (await asyncio.wait_for(reader.readline(), timeout=5)) not in (b"\r\n", b"\n", b""):
                pass
            parts = line.decode("latin-1").split()
            path, _, query = (parts[1] if len(parts) >= 2 else "/").partition("?")
            status, ctype, body = self._route(path, query)
        except Exception as exc:  # never let a bad request take anything down
            log.debug("web request failed: %s", exc)
            status, ctype, body = 400, "text/plain", b"bad request"
        try:
            writer.write(f"HTTP/1.1 {status} {'OK' if status == 200 else 'ERROR'}\r\n"
                         f"Content-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
                         f"Cache-Control: no-store\r\nConnection: close\r\n\r\n".encode() + body)
            await writer.drain()
        finally:
            writer.close()

    def _route(self, path: str, query: str = "") -> tuple[int, str, bytes]:
        alive, info = self.svc.health()
        if path == "/healthz":
            return (200 if alive else 503), "application/json", json.dumps(info).encode()
        if path == "/status":
            eng = self.svc.engine
            body = {"health": info, "market": eng.last_market,
                    "symbols": [r.to_dict() for r in eng.last_results],
                    "alerts": self.svc.store.recent_alerts(30)}
            return 200, "application/json", json.dumps(body, default=str).encode()
        if path == "/":
            return 200, "text/html; charset=utf-8", self._html(info).encode()
        if path == "/screen.json":
            return 200, "application/json", json.dumps(self.svc.store.get("screen:latest")).encode()
        if path == "/screen":
            note = ""
            if "refresh=1" in query.split("&"):
                started = self.svc.start_screen(min_gap_s=REFRESH_GAP_S)
                note = ("Refresh started. This page reloads in 20 seconds." if started else
                        "A screen is already running or ran in the last 5 minutes; showing the latest result.")
            return 200, "text/html; charset=utf-8", self._screen_html(note).encode()
        return 404, "text/plain", b"not found"

    def _html(self, info: dict) -> str:
        eng, cfg = self.svc.engine, self.svc.cfg
        rows = []
        for r in sorted(eng.last_results, key=lambda x: -(x.stock.score if x.stock else -999)):
            o = r.options
            rows.append("<tr>" + "".join(f"<td>{escape(str(c))}</td>" for c in (
                r.symbol, num(r.price), "live" if r.price_is_live else "delayed",
                num(r.day_change_pct, ".2f", True),
                f"{label(r.stock.label)} {num(r.stock.score, '.0f', True)}" if r.stock else "–",
                " ".join(f"{tf}:{num(t.score, '.0f', True)}" for tf, t in (r.stock.timeframes.items() if r.stock else [])),
                f"{label(r.bias.label)} {num(r.bias.score, '.0f', True)}" if r.bias else "–",
                money(r.bias.session_net_premium) if r.bias else "–",
                num(o.pcr_volume) if o else "–", num(o.atm_iv_front, ".1f") if o else "–",
                num(o.rr25, ".1f", True) if o and o.rr25 is not None else "–",
                f"{num(o.put_wall, 'g')} / {num(o.call_wall, 'g')}" if o else "–",
                "; ".join(r.errors or []),
            )) + "</tr>")
        alerts = "".join(
            f"<li><b>{datetime.fromtimestamp(a['ts'], cfg.tz):%m-%d %H:%M}</b> "
            f"[{escape(a['rule'])}] {'' if a['delivered'] else '(pending) '}"
            f"<pre>{escape(strip_html_safe(a['text']))}</pre></li>"
            for a in self.svc.store.recent_alerts(20))
        market = eng.last_market
        mline = (f"Market: <b>{label(market[0])}</b> stock {num(market[1], '.0f', True)}, "
                 f"options {num(market[2], '.0f', True)}") if market else "Market: –"
        body = (f"<h2>MIAS</h2><p>{mline}<br><small>market {escape(info['market'])} · last cycle "
                f"{escape(str(info['last_cycle'].get('at', '–')))} · {'DEGRADED' if info['degraded'] else 'ok'}"
                f"</small></p><div class='wrap'><table class='sort'><thead><tr><th>Sym</th><th>Price</th><th></th>"
                f"<th>Day%</th><th>Trend</th><th>Timeframes</th><th>Options</th><th>Net prem</th><th>P/C</th>"
                f"<th>IV</th><th>RR25</th><th>Put/Call wall</th><th>Notes</th></tr></thead><tbody>{''.join(rows)}"
                f"</tbody></table></div><h3>Recent alerts</h3><ul>{alerts}</ul>")
        return page("MIAS", "overview", body)

    def _screen_html(self, note: str) -> str:
        svc, cfg = self.svc, self.svc.cfg
        res = svc.store.get("screen:latest")
        watch = set(cfg.symbols)
        head = ["<h2>Options screen</h2>"]
        if note:
            head.append(f"<div class='note'>{escape(note)}</div>")
        if svc.screen_running:
            head.append("<div class='note'>A screen is running now (about 15–30 seconds).</div>")
        if svc.screen_error:
            head.append(f"<div class='note bad'>Last run failed: {escape(svc.screen_error)}</div>")
        if not res:
            head.append("<p>No screen yet. It runs at startup and every "
                        f"{cfg.screen_interval_minutes} minutes in market hours. <a href='/screen?refresh=1'>Run now</a></p>")
            return page("MIAS screen", "screen", "".join(head), refresh=20)
        at = datetime.fromisoformat(res["at"]).astimezone(cfg.tz)
        rows = res["rows"]
        passed = [r for r in rows if r["passes"]]
        head.append(
            f"<p><b>{len(passed)}</b> of {res['screened']} pass · as of {at:%a %b %d %H:%M} ET · "
            f"<a href='/screen?refresh=1'>Refresh now</a><br><small>Score = 35% liquidity + 35% tight spreads + "
            f"15% movement + 15% fair IV (percentiles). Must have ≥ $10M premium today, median spread ≤ 5%, "
            f"and weekly expirations. ★ = on your watchlist. Click a column to sort.</small></p>")

        def fmt(r: dict, rank: int | str) -> str:
            sd = "–" if r["spread_day"] is None else f"{r['spread_day']:.1f}"
            ss = "–" if r["spread_swing"] is None else f"{r['spread_swing']:.1f}"
            ivrv = r.get("iv_rv")
            cls = "" if ivrv is None else "good" if ivrv < 0.9 else "bad" if ivrv > 1.25 else ""
            chg = r.get("change_pct")
            cells = [
                (rank, "n"), (("★ " if r["symbol"] in watch else "") + r["symbol"], ""), (r["sector"], ""),
                (f"{r['score']:.0f}", "n"), (f"{r['price']:,.2f}", "n"),
                ("–" if chg is None else f"{chg:+.2f}", "n" + (" good" if (chg or 0) > 0 else " bad" if (chg or 0) < 0 else "")),
                (f"{r['premium'] / 1e6:,.0f}", "n"), (f"{r['oi'] / 1e3:,.0f}", "n"),
                (sd, "n"), (ss, "n"), ("–" if r["rv20"] is None else f"{r['rv20']:.0f}", "n"),
                ("–" if ivrv is None else f"{ivrv:.2f}", "n " + cls),
                ("–" if r["swing_cost"] is None else f"{r['swing_cost']:,.0f}", "n"),
            ]
            if not r["passes"]:
                cells.append((r["why_not"], "mut"))
            tr = " class='watch'" if r["symbol"] in watch else ""
            return f"<tr{tr}>" + "".join(f"<td class='{c}'>{escape(str(v))}</td>" for v, c in cells) + "</tr>"

        cols = ("#", "Symbol", "Sector", "Score", "Price", "Day %", "Premium $M", "OI K", "Spread day %",
                "Spread swing %", "RV20 %", "IV/RV", "Swing contract $")
        thead = "".join(f"<th class='{'n' if c not in ('Symbol', 'Sector') else ''}'>{c}</th>" for c in cols)

        def table(items: list[dict], extra_col: str | None = None, ranked: bool = True) -> str:
            th = thead + (f"<th>{extra_col}</th>" if extra_col else "")
            body = "".join(fmt(r, i if ranked else "–") for i, r in enumerate(items, 1))
            return f"<div class='wrap'><table class='sort'><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>"

        failed = [r for r in rows if not r["passes"]]
        watch_failed = [r for r in failed if r["symbol"] in watch]
        missing_good = [r["symbol"] for r in passed[:20] if r["symbol"] not in watch]
        parts = head + [table(passed)]
        summary = []
        if watch_failed:
            summary.append("Watchlist symbols that fail the screen: " +
                           ", ".join(f"<b>{escape(r['symbol'])}</b> ({escape(r['why_not'])})" for r in watch_failed))
        if missing_good:
            summary.append("Top-20 names not on your watchlist: " + ", ".join(escape(s) for s in missing_good))
        if summary:
            parts.append("<div class='note'>" + "<br>".join(summary) + "</div>")
        parts.append(f"<details><summary>Excluded ({len(failed)})</summary>"
                     f"{table(failed, 'Why excluded', ranked=False)}</details>")
        parts.append("<p><small>Spread = activity-weighted median bid-ask on 0.35–0.65 delta contracts, 1–14 days "
                     "(day) and 50–100 days (swing). RV20 = 20-day realized volatility. IV/RV under 0.9 cheap "
                     "(green), above 1.25 rich (red). Swing contract = one ~0.6 delta call, 50–100 days.</small></p>")
        return page("MIAS screen", "screen", "".join(parts), refresh=20 if svc.screen_running or note else 300)
