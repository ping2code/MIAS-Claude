"""CLI.

  python -m mias run                 run the service (market-hours scheduler, alerts, web status)
  python -m mias once [--send]       one cycle now, print the digest (no state changes; --send also telegrams it)
  python -m mias rank [--top 20]     rank candidate symbols by options activity
  python -m mias screen [--top 30]   best underlyings for options trading (liquidity, spreads, movement, IV)
  python -m mias telegram-test       send a test message
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

from . import config as config_mod
from .notify import Telegram, strip_html

CANDIDATES = [
    "SPY", "QQQ", "IWM", "DIA", "TLT", "GLD", "SLV", "XLF", "XLE", "SMH", "HYG", "EEM", "FXI", "KWEB", "ARKK",
    "NVDA", "TSLA", "AAPL", "AMZN", "META", "MSFT", "GOOGL", "GOOG", "AMD", "AVGO", "NFLX", "PLTR", "MSTR",
    "COIN", "HOOD", "MU", "INTC", "UBER", "SMCI", "BABA", "SOFI", "BA", "JPM", "BAC", "C", "WMT", "COST",
    "ORCL", "CRM", "ADBE", "DIS", "PYPL", "SHOP", "SNOW", "ARM", "TSM", "MARA", "RIOT", "RIVN", "LCID", "NIO",
    "F", "GM", "XOM", "CVX", "UNH", "LLY", "NKE", "SBUX", "MRVL", "QCOM", "CRWD", "PANW", "IBM", "AFRM",
]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mias")
    p.add_argument("--config", default=os.environ.get("MIAS_CONFIG", "config.toml"))
    p.add_argument("--log-level", default=os.environ.get("MIAS_LOG_LEVEL", "INFO"))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    once = sub.add_parser("once")
    once.add_argument("--send", action="store_true", help="also send the digest to Telegram")
    once.add_argument("--symbols", help="comma-separated override")
    rank = sub.add_parser("rank")
    rank.add_argument("--top", type=int, default=20)
    rank.add_argument("--symbols", help="comma-separated candidates (default: built-in list)")
    scr = sub.add_parser("screen")
    scr.add_argument("--top", type=int, default=30)
    scr.add_argument("--add", help="extra comma-separated symbols to include")
    sub.add_parser("telegram-test")
    args = p.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(), stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if getattr(args, "symbols", None) and args.cmd == "once":
        os.environ["MIAS_SYMBOLS"] = args.symbols
    try:
        cfg = config_mod.load(args.config, require_secrets=args.cmd != "telegram-test")
    except config_mod.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.cmd == "run":
        from .app import Service
        asyncio.run(Service(cfg).run())
    elif args.cmd == "once":
        return asyncio.run(_once(cfg, args.send))
    elif args.cmd == "rank":
        syms = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else CANDIDATES
        return asyncio.run(_rank(cfg, syms, args.top))
    elif args.cmd == "screen":
        return asyncio.run(_screen(cfg, args.top, args.add))
    elif args.cmd == "telegram-test":
        return asyncio.run(_telegram_test(cfg))
    return 0


async def _once(cfg, send: bool) -> int:
    from .engine import Engine
    from .massive import MassiveClient
    from .store import Store

    store = Store(cfg.db_path)
    async with MassiveClient(cfg.api_key, cfg.base_url, timeout=cfg.request_timeout, max_retries=cfg.max_retries,
                             max_concurrency=cfg.max_concurrency) as client:
        engine = Engine(cfg, client, store, None)
        now = datetime.now(timezone.utc)
        await engine.cycle(now, dry_run=True)
        text = engine.digest_text("MIAS snapshot", now)
    print(strip_html(text))
    print("\ncycle:", engine.last_cycle)
    for r in engine.last_results:
        for e in r.errors or []:
            print(f"  {r.symbol}: {e}")
    if send:
        await _send(cfg, text)
    store.close()
    return 0 if engine.healthy_cycle() else 1


async def _rank(cfg, symbols: list[str], top: int) -> int:
    from .massive import MassiveClient

    today = datetime.now(cfg.tz).date()
    async with MassiveClient(cfg.api_key, cfg.base_url, timeout=cfg.request_timeout, max_retries=cfg.max_retries,
                             max_concurrency=cfg.max_concurrency) as client:
        snaps = await client.snapshots(symbols)

        async def one(sym):
            price = (snaps.get(sym, {}).get("session") or {}).get("price")
            if not price:
                return sym, None
            try:
                chain, _ = await client.option_chain(sym, spot=price, today=today, max_dte=cfg.chain_max_dte,
                                                     strike_pct=cfg.chain_strike_pct, max_pages=60)
            except Exception as exc:
                print(f"{sym}: {exc}", file=sys.stderr)
                return sym, None
            vol = sum((c.get("day") or {}).get("volume") or 0 for c in chain)
            prem = sum(((c.get("day") or {}).get("volume") or 0) * ((c.get("day") or {}).get("vwap") or 0) * 100
                       for c in chain)
            oi = sum(c.get("open_interest") or 0 for c in chain)
            return sym, (vol, prem, oi, len(chain))

        rows = [x for x in await asyncio.gather(*(one(s) for s in symbols)) if x[1]]
    rows.sort(key=lambda x: -x[1][1])
    print(f"Options activity (latest session, expiries <= {cfg.chain_max_dte}d, strikes +/-"
          f"{cfg.chain_strike_pct:.0%}), ranked by premium traded\n")
    print(f"{'#':>2} {'SYM':<6}{'VOLUME':>12}{'PREMIUM':>14}{'OPEN INT':>13}{'CONTRACTS':>11}")
    for i, (s, (v, pr, oi, n)) in enumerate(rows, 1):
        mark = "*" if i <= top else " "
        print(f"{i:>2}{mark}{s:<6}{v:>12,.0f}{pr / 1e6:>12,.0f}M{oi:>13,.0f}{n:>11}")
    print("\nTop", top, ":", ", ".join(s for s, _ in rows[:top]))
    return 0


async def _screen(cfg, top: int, add: str | None) -> int:
    from . import screen
    from .massive import MassiveClient

    extra = [s.strip().upper() for s in (add or "").split(",") if s.strip()] + list(cfg.symbols)
    now = datetime.now(cfg.tz)
    async with MassiveClient(cfg.api_key, cfg.base_url, timeout=cfg.request_timeout, max_retries=cfg.max_retries,
                             max_concurrency=10) as client:
        rows = await screen.collect(client, screen.universe(extra), cfg.tz, now.date())
    print(screen.report(screen.score(rows), top, now))
    return 0


async def _telegram_test(cfg) -> int:
    if not (cfg.telegram_token and cfg.telegram_chat_id):
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set", file=sys.stderr)
        return 2
    await _send(cfg, "🔔 <b>MIAS</b> test message: Telegram delivery works.")
    print("sent")
    return 0


async def _send(cfg, text: str) -> None:
    tg = Telegram(cfg.telegram_token, cfg.telegram_chat_id)
    try:
        await tg.send(text)
    finally:
        await tg.close()


if __name__ == "__main__":
    sys.exit(main())
