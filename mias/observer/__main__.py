"""python -m mias.observer {run, replay, report, validate, status}"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import date, datetime
from pathlib import Path

REPLAY_CSV = Path("data/exports/replay_trades_4yr_2022-10-28_to_2026-10-27_v2.csv")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="mias.observer", description="MIAS Live Observer + Early Watch (shadow mode)")
    p.add_argument("--config", default="config.toml")
    p.add_argument("--log-level", default="INFO")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="live shadow observer (WebSocket AM)")
    r = sub.add_parser("replay", help="historical replay with the live code on REST 1-minute bars")
    r.add_argument("--days", type=int, default=40)
    r.add_argument("--end", default=None)
    r.add_argument("--db", default="data/observer/replay.db")
    rp = sub.add_parser("report", help="score a live or replay database")
    rp.add_argument("--db", default="data/observer/observer.db")
    rp.add_argument("--out", default=None)
    rp.add_argument("--title", default=None)
    v = sub.add_parser("validate", help="REST vs WebSocket bars for a day (Phase A)")
    v.add_argument("--date", default=None)
    sub.add_parser("status", help="last status line, feed events and today's watches")
    args = p.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    from .. import config
    from . import settings as settings_mod
    cfg = config.load(args.config)
    s = settings_mod.load(args.config, list(cfg.daytrade.symbols))

    if args.cmd == "run":
        if not s.enabled:
            print("[observer] enabled = false")
            return 0
        from .service import main as run_main
        asyncio.run(run_main(cfg, s))
        return 0
    if args.cmd == "replay":
        from .replay import replay
        from .report import build
        from .sources import MiasSource
        from .log import ObserverDB
        end = date.fromisoformat(args.end) if args.end else date.today()
        src = MiasSource(Path(cfg.db_path), cfg, REPLAY_CSV)
        db_path = Path(args.db)
        summary = asyncio.run(replay(cfg, s, args.days, end, db_path, s.cache_dir, src))
        print(json.dumps(summary)[:400])
        db = ObserverDB(db_path)
        print(build(db, cfg.tz, f"Early Watch v1 — historical replay ({len(summary['days'])} days)",
                    db_path.parent / "replay_report")[:3000])
        db.close()
        return 0
    if args.cmd == "report":
        from .log import ObserverDB
        from .report import build
        db = ObserverDB(Path(args.db))
        out = Path(args.out) if args.out else Path(args.db).parent / (Path(args.db).stem + "_report")
        print(build(db, cfg.tz, args.title or f"Early Watch v1 — {Path(args.db).name}", out))
        db.close()
        return 0
    if args.cmd == "validate":
        from ..massive import MassiveClient
        from .log import ObserverDB
        from .validate import validate
        day = date.fromisoformat(args.date) if args.date else date.today()
        db = ObserverDB(s.db_path)

        async def go():
            async with MassiveClient(cfg.api_key, cfg.base_url, timeout=cfg.request_timeout,
                                     max_retries=cfg.max_retries) as client:
                return await validate(client, db, s.symbols, day, cfg.tz)
        text = asyncio.run(go())
        out = Path("data/observer/validation") / f"{day}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        print(text)
        db.close()
        return 0
    if args.cmd == "status":
        from .log import ObserverDB
        db = ObserverDB(s.db_path)
        print(json.dumps(db.get("status"), indent=1))
        for ts, kind, detail in db.db.execute("SELECT ts, kind, detail FROM feed_events ORDER BY ts DESC LIMIT 15"):
            print(f"{datetime.fromtimestamp(ts):%m-%d %H:%M:%S} {kind} {detail}")
        today = date.today().isoformat()
        for w in db.watches("day = ?", (today,)):
            print(w["watch_id"], w["structure_type"], w["price"], w["state"], w["end_reason"])
        db.close()
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
