import asyncio
import dataclasses
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from mias import config as config_mod
from mias.engine import Engine
from mias.massive import MassiveClient, MassiveError
from mias.models import SymbolResult
from mias.notify import Dispatcher, Telegram, split_message
from mias.signals import _confirm, evaluate
from mias.store import Store

from fakes import FakeMassive

ET = ZoneInfo("America/New_York")
TODAY = date(2026, 10, 7)
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    monkeypatch.setenv("MIAS_SYMBOLS", "AAA,BBB,CCC")
    monkeypatch.setenv("MIAS_DB_PATH", str(tmp_path / "t.db"))
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.setenv(k, "")
    cfg = config_mod.load(ROOT / "config.toml")
    return dataclasses.replace(cfg, thin_options_volume=0)


def run(coro):
    return asyncio.run(coro)


# ---- config ------------------------------------------------------------------------------------

def test_config_loads(cfg):
    assert cfg.symbols == ["AAA", "BBB", "CCC"]
    assert "test-key" not in repr(cfg)


def test_config_requires_key(tmp_path, monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    (tmp_path / "config.toml").write_text('[watchlist]\nsymbols=["SPY"]\n')
    with pytest.raises(config_mod.ConfigError):
        config_mod.load(tmp_path / "config.toml")


# ---- massive client ----------------------------------------------------------------------------

def test_client_retries_then_succeeds():
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        return httpx.Response(503) if n["i"] < 3 else httpx.Response(200, json={"ok": 1})

    async def go():
        async with MassiveClient("k", transport=httpx.MockTransport(handler), max_retries=4) as c:
            c._backoff = _no_sleep
            return await c.get("/x")
    assert run(go()) == {"ok": 1} and n["i"] == 3


def test_client_does_not_retry_403_and_hides_key():
    async def go():
        async with MassiveClient("secret-key", transport=httpx.MockTransport(
                lambda r: httpx.Response(403, json={"message": "not entitled"}))) as c:
            await c.get("/x")
    with pytest.raises(MassiveError) as e:
        run(go())
    assert e.value.status == 403 and "secret-key" not in str(e.value)


def test_client_refuses_foreign_pagination_host():
    async def go():
        async with MassiveClient("k", transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"results": [1], "next_url": "https://evil.example/x"}))) as c:
            return await c.paginate("/x", {}, 5)
    with pytest.raises(MassiveError):
        run(go())


async def _no_sleep(*a, **k):
    return None


# ---- signals -----------------------------------------------------------------------------------

def test_confirm_requires_consecutive_cycles():
    slot = {}
    assert _confirm(slot, "bull", 2) is None          # initialises silently
    assert _confirm(slot, "bear", 2) is None
    assert _confirm(slot, "bull", 2) is None          # back: pending reset
    assert _confirm(slot, "bear", 2) is None
    assert _confirm(slot, "bear", 2) == ("bull", "bear")
    assert slot["cls"] == "bear"


def _result(stock_score, opt_score, price=100.0, walls=(105, 95)):
    from mias.options import OptionsBias, OptionsSnapshot
    from mias.trend import StockTrend, TimeframeTrend, label_for
    lab = label_for(stock_score, 60, 25)
    st = StockTrend(stock_score, lab, {"1h": TimeframeTrend("1h", stock_score, lab, price, 0)}, price, 0)
    snap = OptionsSnapshot(1, price, 1, 1, 1, 1, 1, 1, 1.0, 1.0, 30.0, 30.0, 0.0, 0, walls[0], walls[1])
    olab = label_for(opt_score, 60, 25).replace("_up", "_bullish").replace("_down", "_bearish")
    olab = {"up": "bullish", "down": "bearish"}.get(olab, olab)
    return SymbolResult("AAA", stock=st, options=snap, bias=OptionsBias(opt_score, olab, {"x": 1}, 0, 0),
                        live_price=price, stock_price=price, prev_close=99)


def test_trend_flip_alignment_divergence_and_cooldown(cfg):
    state, t = {}, 1_000_000.0
    assert evaluate(_result(40, 40), state, cfg, t, []) == []          # baseline
    assert evaluate(_result(-40, 40), state, cfg, t + 150, []) == []   # 1st bear cycle: unconfirmed
    rs = evaluate(_result(-40, -50), state, cfg, t + 300, [])          # confirmed stock flip
    rules = [r.rule for r in rs]
    assert "trend_change" in rules
    rs = evaluate(_result(-40, -50), state, cfg, t + 450, [])          # options confirmed bear -> aligned
    assert {"options_flip", "aligned"} <= {r.rule for r in rs}
    # Quick flip back is suppressed by the cooldown even once confirmed.
    evaluate(_result(40, -50), state, cfg, t + 600, [])
    rs = evaluate(_result(40, -50), state, cfg, t + 750, [])
    assert "trend_change" not in {r.rule for r in rs}
    assert "divergence" in {r.rule for r in rs}


def test_wall_break_and_iv_spike(cfg):
    state, t = {}, 1_000_000.0
    evaluate(_result(40, 40, price=104), state, cfg, t, [])
    rs = evaluate(_result(40, 40, price=106), state, cfg, t + 150, [(t - 600, 26.0)])
    rules = {r.rule for r in rs}
    assert "wall_break" in rules and "iv_spike" in rules


# ---- telegram ----------------------------------------------------------------------------------

def test_split_message():
    text = "\n".join("x" * 100 for _ in range(100))
    chunks = split_message(text)
    assert all(len(c) <= 4000 for c in chunks) and "".join(chunks).count("x") == 10000


def test_dispatcher_keeps_failed_alerts_and_retries(tmp_path):
    store = Store(tmp_path / "a.db")
    store.enqueue("t", "hello <b>x</b>")
    state = {"fail": True, "sent": []}

    def handler(req):
        if state["fail"]:
            return httpx.Response(500, json={"description": "down"})
        state["sent"].append(json.loads(req.content)["text"])
        return httpx.Response(200, json={"ok": True})

    async def go():
        tg = Telegram("tok", "chat", transport=httpx.MockTransport(handler))
        d = Dispatcher(store, tg)
        assert await d.flush() == 0 and len(store.pending()) == 1 and "tok" not in (d.last_error or "")
        state["fail"] = False
        assert await d.flush() == 1 and store.pending() == []
        await tg.close()
    run(go())
    assert state["sent"] == ["hello <b>x</b>"]


# ---- engine integration ------------------------------------------------------------------------

def _engine(cfg, fake):
    client = MassiveClient("test-key", transport=httpx.MockTransport(fake.handler), max_retries=0)
    store = Store(cfg.db_path)
    return Engine(cfg, client, store, Dispatcher(store, None)), store, client


def test_engine_cycles_isolate_failures_and_alert(cfg):
    now = datetime.combine(TODAY, time(15, 0), ET)
    fake = FakeMassive(TODAY, now, {"AAA": 100.0, "BBB": 200.0, "CCC": 50.0})
    fake.fail.add("CCC")
    eng, store, client = _engine(cfg, fake)

    async def go():
        r1 = await eng.cycle(now)
        by = {r.symbol: r for r in r1}
        assert by["AAA"].stock.label in ("up", "strong_up")
        assert by["AAA"].live_price == pytest.approx(99.7)              # real-time stock feed
        assert by["AAA"].options.window is None
        assert not by["CCC"].ok and by["CCC"].errors
        assert eng.last_cycle["failed"] == ["CCC"] and eng.healthy_cycle()

        # Heavy bearish flow on AAA for three cycles: puts bought, calls sold.
        for i in range(1, 4):
            fake.now = now + timedelta(minutes=2.5 * i)
            fake.volumes["AAA"] = {("put", 100.0, 8): 10 + 12000 * i, ("call", 100.0, 8): 10 + 9000 * i}
            fake.last_at["AAA"] = {("put", 100.0, 8): "buy", ("call", 100.0, 8): "sell"}
            res = await eng.cycle(fake.now)
        aaa = next(r for r in res if r.symbol == "AAA")
        assert aaa.bias.score < -25 and aaa.bias.label.endswith("bearish")
        await client.close()

    run(go())
    alerts = store.recent_alerts()
    texts = " ".join(a["text"] for a in alerts)
    assert any(a["symbol"] == "AAA" for a in alerts)
    assert "Options bias" in texts and "Block" in texts
    assert all(a["delivered"] for a in alerts)       # no Telegram: logged and marked
    assert store.history("AAA", 0) and not store.history("CCC", 0)


def test_dry_run_writes_nothing(cfg):
    now = datetime.combine(TODAY, time(15, 0), ET)
    fake = FakeMassive(TODAY, now, {"AAA": 100.0, "BBB": 200.0, "CCC": 50.0})
    eng, store, client = _engine(cfg, fake)

    async def go():
        await eng.cycle(now, dry_run=True)
        await client.close()
    run(go())
    assert store.get("vol:AAA") is None and store.history("AAA", 0) == [] and store.recent_alerts() == []
    assert "AAA" in eng.digest_text("t", now)


# ---- scheduler ---------------------------------------------------------------------------------

def test_service_digest_and_closing_summary(cfg):
    from mias.app import Service
    t0 = datetime.combine(TODAY, time(11, 31), ET)
    fake = FakeMassive(TODAY, t0, {"AAA": 100.0, "BBB": 200.0, "CCC": 50.0})
    svc = Service(cfg, transport=httpx.MockTransport(fake.handler))
    svc.client._retries = 0

    async def go():
        await svc._tick(t0)                                   # open: cycle + 11:30 digest
        await svc._tick(t0 + timedelta(minutes=3))            # no duplicate digest
        assert svc.market_state == "open"
        fake.handler_status = "closed"
        orig = fake.handler

        def closed(req):
            if req.url.path == "/v1/marketstatus/now":
                return httpx.Response(200, json={"market": "closed"})
            return orig(req)
        svc.client._http._transport = httpx.MockTransport(closed)
        await svc._tick(datetime.combine(TODAY, time(16, 1), ET))   # closing summary once
        await svc._tick(datetime.combine(TODAY, time(16, 3), ET))
        await svc._tick(datetime.combine(TODAY, time(20, 0), ET))   # outside window: no cycle
        await svc.client.close()
    run(go())
    rules = [a["rule"] for a in svc.store.recent_alerts()]
    texts = [a["text"] for a in svc.store.recent_alerts() if a["rule"] == "digest"]
    assert rules.count("digest") == 2
    assert any("MIAS digest 11:30" in t for t in texts) and any("Closing summary" in t for t in texts)


def test_service_degraded_and_recovered(cfg):
    from mias.app import Service
    t0 = datetime.combine(TODAY, time(12, 0), ET)
    fake = FakeMassive(TODAY, t0, {"AAA": 100.0, "BBB": 200.0, "CCC": 50.0})
    fake.fail.update({"AAA", "BBB", "CCC"})
    svc = Service(cfg, transport=httpx.MockTransport(fake.handler))
    svc.client._retries = 0

    async def go():
        for i in range(3):
            await svc._tick(t0 + timedelta(minutes=3 * i))
        assert svc.degraded
        fake.fail.clear()
        await svc._tick(t0 + timedelta(minutes=10))
        assert not svc.degraded
        await svc.client.close()
    run(go())
    texts = " ".join(a["text"] for a in svc.store.recent_alerts() if a["rule"] == "system")
    assert "degraded" in texts and "recovered" in texts


def test_no_closing_summary_from_a_previous_day(cfg):
    from mias.app import Service
    t0 = datetime.combine(TODAY, time(12, 0), ET)
    fake = FakeMassive(TODAY, t0, {"AAA": 100.0, "BBB": 200.0, "CCC": 50.0})
    svc = Service(cfg, transport=httpx.MockTransport(fake.handler))
    svc.client._retries = 0
    orig = fake.handler

    def closed(req):
        if req.url.path == "/v1/marketstatus/now":
            return httpx.Response(200, json={"market": "closed"})
        return orig(req)

    async def go():
        await svc._tick(t0)
        svc.market_state = "closed"                       # e.g. process kept running overnight
        svc.client._http._transport = httpx.MockTransport(closed)
        await svc._tick(datetime.combine(TODAY + timedelta(days=1), time(16, 5), ET))   # holiday next day
        await svc.client.close()
    run(go())
    assert not any("Closing summary" in a["text"] for a in svc.store.recent_alerts())


# ---- verdict -----------------------------------------------------------------------------------

def _vres(tf_scores: dict, opt_score: float, thin: bool = False):
    from mias.options import OptionsBias, OptionsSnapshot
    from mias.trend import StockTrend, TimeframeTrend, label_for
    tfs = {tf: TimeframeTrend(tf, s, label_for(s, 60, 25), 100, 0) for tf, s in tf_scores.items()}
    comp = sum(tf_scores.values()) / len(tf_scores)
    st = StockTrend(comp, label_for(comp, 60, 25), tfs, 100, 0)
    snap = OptionsSnapshot(1, 100, 1, 1, 1, 1, 1, 1, 1.0, 1.0, 30, 30, 0, 0, None, None, thin=thin)
    olab = {"up": "bullish", "down": "bearish"}.get(label_for(opt_score, 60, 25),
                                                    label_for(opt_score, 60, 25).replace("_up", "_bullish")
                                                    .replace("_down", "_bearish"))
    return SymbolResult("X", stock=st, options=snap, bias=OptionsBias(opt_score, olab, {"x": 1}, 0, 0),
                        stock_price=100)


def test_verdict_levels():
    from mias.verdict import verdict
    tfs = ["1w", "1d", "1h"]
    assert verdict(_vres({"1w": 50, "1d": 40, "1h": 30}, 45), tfs).level == "HIGH"
    assert verdict(_vres({"1w": 50, "1d": 40, "1h": 30}, 0), tfs).level == "MEDIUM"
    assert verdict(_vres({"1w": 50, "1d": 40, "1h": 30}, 45, thin=True), tfs).level == "MEDIUM"
    assert verdict(_vres({"1w": 50, "1d": 40, "1h": 0}, 10), tfs).level == "MEDIUM"
    assert verdict(_vres({"1w": 50, "1d": 40, "1h": 30}, -50), tfs).level == "CONFLICT"
    assert verdict(_vres({"1w": 60, "1d": 40, "1h": -60}, 0), tfs).level == "LOW"
    assert verdict(_vres({"1w": 5, "1d": -5, "1h": 0}, 50), tfs).level == "LOW"


# ---- telegram commands -------------------------------------------------------------------------

def test_parse_commands():
    from mias.bot import parse
    assert parse("/trend nvda mu") == ("trend", ["NVDA", "MU"])
    assert parse("/t@MyBot NVDA") == ("t", ["NVDA"])
    assert parse("nvda") == ("trend", ["NVDA"])
    assert parse("$NVDA, MU?") == ("trend", ["$NVDA", "MU"])
    assert parse("what is the market doing today") == ("unknown", [])
    assert parse("/market") == ("market", [])


def _bot_service(cfg, monkeypatch):
    from mias.app import Service
    from mias.bot import CommandBot
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    cfg = dataclasses.replace(cfg, telegram_token="tok", telegram_chat_id="42")
    now = datetime.combine(TODAY, time(15, 0), ET)
    fake = FakeMassive(TODAY, now, {"AAA": 100.0, "BBB": 200.0, "CCC": 50.0, "ZZZ": 30.0})
    svc = Service(cfg, transport=httpx.MockTransport(fake.handler))
    svc.client._retries = 0
    bot = CommandBot(svc)
    sent = []

    async def capture(html):
        sent.append(html)
    bot.reply = capture
    return svc, bot, sent


def test_bot_trend_reply_and_auth(cfg, monkeypatch):
    svc, bot, sent = _bot_service(cfg, monkeypatch)

    async def go():
        await bot._handle({"chat": {"id": 999}, "text": "/trend AAA"})          # stranger: ignored
        assert sent == []
        await bot._handle({"chat": {"id": 42}, "text": "aaa zzz"})              # watched + unwatched
        await bot._handle({"chat": {"id": 42}, "text": "/trend"})
        await bot._handle({"chat": {"id": 42}, "text": "hello there friend"})
        await bot._handle({"chat": {"id": 42}, "text": "/market"})
        await bot._handle({"chat": {"id": 42}, "text": "/help"})
        await svc.client.close()
        await bot.close()
    run(go())
    aaa = next(m for m in sent if "<b>AAA</b>" in m)
    zzz = next(m for m in sent if "<b>ZZZ</b>" in m)
    assert "Swing:" in aaa and "Intraday:" in aaa and "Confidence" in aaa and "Day trade" in aaa
    assert "not on the watchlist" in zzz and "not on the watchlist" not in aaa
    assert any("Send a symbol" in m for m in sent)
    assert any("didn't get that" in m for m in sent)
    assert any("MARKET" in m for m in sent) and any("Reading the answer" in m for m in sent)
    # On-demand requests never touch alert state.
    assert svc.store.get("state:AAA") is None and svc.store.get("vol:AAA") is None


def test_bot_unknown_symbol(cfg, monkeypatch):
    svc, bot, sent = _bot_service(cfg, monkeypatch)

    async def go():
        await bot.dispatch("trend", ["NOPE"])
        await svc.client.close()
        await bot.close()
    run(go())
    assert "no data" in sent[0] and "NOPE" in sent[0]


def test_delayed_feed_uses_options_implied_price(cfg):
    cfg = dataclasses.replace(cfg, stock_delay_minutes=15)
    now = datetime.combine(TODAY, time(15, 0), ET)
    fake = FakeMassive(TODAY, now, {"AAA": 100.0, "BBB": 200.0, "CCC": 50.0})
    eng, store, client = _engine(cfg, fake)

    async def go():
        r = await eng.analyze_one("AAA", now)
        await client.close()
        return r
    r = run(go())
    assert r.stock_price == pytest.approx(99.7) and r.live_price == pytest.approx(100.0, abs=0.01)
    assert r.price_is_live


# ---- dashboard screen ---------------------------------------------------------------------------

def test_screen_page_refresh_and_keep_last_good(cfg, monkeypatch):
    from mias import screen
    from mias.app import Service
    from mias.web import WebServer
    now = datetime.combine(TODAY, time(15, 0), ET)
    fake = FakeMassive(TODAY, now, {"AAA": 100.0, "BBB": 200.0, "CCC": 50.0})
    svc = Service(cfg, transport=httpx.MockTransport(fake.handler))
    svc.client._retries = 0
    web = WebServer(svc)
    monkeypatch.setattr(screen, "universe", lambda extra: {"AAA": "Tech", "BBB": "Tech", "CCC": "Energy"})

    async def go():
        status, _, body = web._route("/screen")
        assert status == 200 and b"No screen yet" in body
        monkeypatch.setattr(screen, "MIN_ROWS_TO_SAVE", 10)          # too few rows -> nothing saved
        assert svc.start_screen()
        await svc._screen_task
        assert svc.store.get("screen:latest") is None and "too little data" in svc.screen_error
        monkeypatch.setattr(screen, "MIN_ROWS_TO_SAVE", 1)
        svc.screen_started = 0
        _, _, body = web._route("/screen", "refresh=1")
        assert b"Refresh started" in body
        await svc._screen_task
        _, _, body = web._route("/screen", "refresh=1")             # within 5 minutes: refused
        assert b"already running or ran in the last 5 minutes" in body
        await svc.client.close()
    run(go())
    res = svc.store.get("screen:latest")
    assert res["screened"] == 3 and {r["symbol"] for r in res["rows"]} == {"AAA", "BBB", "CCC"}
    _, ctype, body = web._route("/screen")
    html = body.decode()
    assert "Options screen" in html and "★ AAA" in html and "Excluded" in html
    _, ctype, body = web._route("/screen.json")
    assert ctype == "application/json" and json.loads(body)["screened"] == 3
    _, _, body = web._route("/")
    assert b"Options screen" in body                                # nav link on the overview


def test_screen_due(cfg):
    from mias.app import Service
    svc = Service(cfg)
    now = datetime.combine(TODAY, time(12, 0), ET)
    assert svc._screen_due(now)
    svc.store.put("screen:latest", {"at": (now - timedelta(minutes=30)).isoformat(), "rows": []})
    assert not svc._screen_due(now)
    assert svc._screen_due(now + timedelta(minutes=cfg.screen_interval_minutes))
    run(svc.client.close())
