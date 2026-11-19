"""python -m mias.early_watch_v3 {run, replay, report, render, telegram-test, status}"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import date, datetime
from pathlib import Path


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="mias.early_watch_v3", description="V3 EARLY WATCH (research)")
    p.add_argument("--config", default="config.toml", help="MIAS config (secrets, timezone, MIAS db path)")
    p.add_argument("--v3-config", default="early_watch_v3.toml")
    p.add_argument("--log-level", default="INFO")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="live V3 process")
    r = sub.add_parser("replay", help="replay stored sessions with the live V3 code (own replay database)")
    r.add_argument("days", nargs="+")
    r.add_argument("--db", default="data/early_watch_v3/replay.db")
    r.add_argument("--no-options", action="store_true")
    rp = sub.add_parser("report", help="daily V3 report + V2 comparison")
    rp.add_argument("--day", default=None)
    rp.add_argument("--db", default=None)
    rd = sub.add_parser("render", help="print the exact V3 Telegram text of stored Watches")
    rd.add_argument("--db", default="data/early_watch_v3/replay.db")
    rd.add_argument("--day", required=True)
    rd.add_argument("--sym", required=True)
    rd.add_argument("--side", choices=["bull", "bear"], default=None)
    rd.add_argument("--near", default=None, help="HH:MM ET")
    sub.add_parser("telegram-test", help="one clearly labelled delivery test to the V3 chat")
    sub.add_parser("status", help="last status and feed lines")
    args = p.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    from .. import config as mias_config
    from . import config as v3config
    cfg = mias_config.load(args.config)
    v3 = v3config.load(args.v3_config)

    if args.cmd == "run":
        from .service import main as run_main
        asyncio.run(run_main(cfg, v3))           # enabled = false keeps the process idle (hot switch), never exits
        return 0
    if args.cmd == "replay":
        from ..massive import MassiveClient
        from .replay import replay_day

        async def go():
            async with MassiveClient(cfg.api_key, cfg.base_url, timeout=cfg.request_timeout,
                                     max_retries=cfg.max_retries, max_concurrency=2) as client:
                outs = []
                for d in args.days:
                    db = Path(args.db) if len(args.days) == 1 else Path(args.db).with_name(
                        f"{Path(args.db).stem}_{d}.db")
                    outs.append(await replay_day(cfg, v3, date.fromisoformat(d), db, client,
                                                 options=not args.no_options))
                return outs
        for o in asyncio.run(go()):
            print(json.dumps({k: v for k, v in o.items() if k != "report"}))
            print(o["report"] or "")
        return 0
    if args.cmd == "report":
        from .report import build
        from .store import V3DB
        db = V3DB(Path(args.db) if args.db else v3.db_path)
        day = args.day or date.today().isoformat()
        print(build(db, cfg.tz, day, v3.v2_db))
        db.close()
        return 0
    if args.cmd == "render":
        from .store import V3DB
        db = V3DB(Path(args.db))
        ws = db.watches("day = ? AND sym = ?", (args.day, args.sym.upper()))
        if args.side:
            ws = [w for w in ws if w["side"] == args.side]
        if args.near:
            hh, mm = (int(x) for x in args.near.split(":"))
            target = datetime.combine(date.fromisoformat(args.day), datetime.min.time().replace(hour=hh, minute=mm),
                                      cfg.tz).timestamp() * 1000
            ws = sorted(ws, key=lambda w: abs(w["watch_at"] - target))[:1]
        for w in ws:
            print(f"--- {w['watch_id']} · {w['state']} ({w['end_reason']}) · V3 policy {w['v3_verdict']} · "
                  f"v2_would_send {bool(w['v2_would_send'])}")
            print(w["text"] or f"(no text: {w['render_error']})")
        db.close()
        return 0
    if args.cmd == "telegram-test":
        from .service import make_sender
        sender, chats = make_sender(cfg, v3)
        if sender is None:
            print("no bot token / V3 chat configured")
            return 1
        text = ("🧭 V3 EARLY WATCH · delivery test (not a Watch)\n"
                "This chat receives V3 EARLY WATCH research messages (all three families: V3-A Structure, V3-B VWAP, "
                "V3-C OR) from the next session, 09:45–15:00 ET.\n"
                "WATCH ONLY — NOT A TRADE SIGNAL")

        async def go():
            try:
                return await sender.send(text)
            finally:
                await sender.close()
        ids = asyncio.run(go())
        print(f"sent to {len(ids)} chat(s), message ids {', '.join(ids)}")
        return 0
    if args.cmd == "status":
        from .store import V3DB
        db = V3DB(v3.db_path)
        print(json.dumps(db.get("status"), indent=1))
        for ts, kind, detail in db.db.execute("SELECT ts, kind, detail FROM v3_feed ORDER BY ts DESC LIMIT 15"):
            print(f"{datetime.fromtimestamp(ts):%m-%d %H:%M:%S} {kind} {detail}")
        db.close()
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
