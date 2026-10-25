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
        d = Dispatcher(store, tg, ["chat"])
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
    cfg = dataclasses.replace(cfg, alert_mode="all")
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
    cfg = dataclasses.replace(cfg, telegram_token="tok", telegram_chat_id="42", telegram_alert_chats=["42"],
                              telegram_allowed_users=["42"], telegram_allowed_chats=["42"])
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



# ---- strong-alignment mode ---------------------------------------------------------------------

def _aligned(swing, intra, opt, thin=False):
    from mias.options import OptionsBias, OptionsSnapshot
    from mias.trend import StockTrend, TimeframeTrend, label_for
    lab = lambda x: label_for(x, 60, 25)
    st = StockTrend(swing, lab(swing), {"1d": TimeframeTrend("1d", swing, lab(swing), 100, 0)}, 100, 0,
                    "swing", {"swing": (swing, lab(swing)), "intraday": (intra, lab(intra))})
    snap = OptionsSnapshot(1, 100, 1, 1, 1, 1, 1, 1, 1.0, 1.0, 30, 30, 0, 0, 105, 95, thin=thin)
    olab = {"up": "bullish", "down": "bearish", "strong_up": "strong_bullish", "strong_down": "strong_bearish"}.get(lab(opt), "neutral")
    return SymbolResult("AAA", stock=st, options=snap, bias=OptionsBias(opt, olab, {"x": 1}, 0, 0), stock_price=100)


def test_strong_side():
    from mias.signals import strong_side
    assert strong_side(_aligned(70, 65, 30)) == "bull"
    assert strong_side(_aligned(-70, -65, -80)) == "bear"
    assert strong_side(_aligned(70, 50, 30)) == "none"          # intraday only UP, not STRONG
    assert strong_side(_aligned(70, 65, 10)) == "none"          # options neutral
    assert strong_side(_aligned(70, 65, 30, thin=True)) == "none"
    assert strong_side(_aligned(70, 65, -30)) == "none"


def test_strong_alignment_confirm_flicker_end_and_cooldown(cfg):
    from mias.signals import evaluate_strong
    state, t = {}, 1_000_000.0
    assert evaluate_strong(_aligned(70, 65, 30), state, cfg, t) == []              # 1st aligned reading
    rs = evaluate_strong(_aligned(70, 65, 30), state, cfg, t + 150)                # confirmed
    assert [r.rule for r in rs] == ["strong_alignment"] and "STRONG BULLISH" in rs[0].text
    assert evaluate_strong(_aligned(70, 50, 30), state, cfg, t + 300) == []        # one weak reading: no change
    assert evaluate_strong(_aligned(70, 65, 30), state, cfg, t + 450) == []        # flicker absorbed
    evaluate_strong(_aligned(70, 50, 30), state, cfg, t + 600)
    rs = evaluate_strong(_aligned(70, 50, 30), state, cfg, t + 750)                # confirmed end
    assert [r.rule for r in rs] == ["alignment_ended"]
    evaluate_strong(_aligned(70, 65, 30), state, cfg, t + 900)
    assert evaluate_strong(_aligned(70, 65, 30), state, cfg, t + 1050) == []       # re-entry inside cooldown
    quiet = dataclasses.replace(cfg, alignment_end_alert=False)
    s2 = {}
    for i in range(2):
        evaluate_strong(_aligned(-70, -65, -40), s2, quiet, t + 150 * i)
    evaluate_strong(_aligned(0, 0, 0), s2, quiet, t + 300)
    assert evaluate_strong(_aligned(0, 0, 0), s2, quiet, t + 450) == []           # end alert switched off


def test_strong_mode_suppresses_other_alerts(cfg):
    now = datetime.combine(TODAY, time(15, 0), ET)
    fake = FakeMassive(TODAY, now, {"AAA": 100.0, "BBB": 200.0, "CCC": 50.0})
    eng, store, client = _engine(cfg, fake)

    async def go():
        await eng.cycle(now)
        for i in range(1, 4):       # the same heavy flow that triggers blocks and flips in "all" mode
            fake.now = now + timedelta(minutes=2.5 * i)
            fake.volumes["AAA"] = {("put", 100.0, 8): 10 + 12000 * i, ("call", 100.0, 8): 10 + 9000 * i}
            fake.last_at["AAA"] = {("put", 100.0, 8): "buy", ("call", 100.0, 8): "sell"}
            await eng.cycle(fake.now)
        await client.close()
    run(go())
    rules = {a["rule"] for a in store.recent_alerts()}
    assert not rules & {"block", "options_flip", "trend_change", "divergence", "market"}


# ---- NDX-style instruments and scheduled reports -----------------------------------------------

def _ndx_cfg(cfg):
    from mias.config import Instrument
    return dataclasses.replace(cfg, symbols=["AAA", "NDX"], instruments={
        "NDX": Instrument("I:NDX", "QQQ", "I:NDX", 0.06, "trend from QQQ bars")}, reports={"NDX": 10})


def test_instrument_uses_proxy_trend_and_option_implied_price(cfg):
    cfg = _ndx_cfg(cfg)
    now = datetime.combine(TODAY, time(11, 0), ET)
    fake = FakeMassive(TODAY, now, {"AAA": 100.0, "QQQ": 750.0, "I:NDX": 300.0})
    eng, store, client = _engine(cfg, fake)

    async def go():
        res = await eng.cycle(now)
        await client.close()
        return {r.symbol: r for r in res}
    by = run(go())
    ndx = by["NDX"]
    assert ndx.ok and ndx.stock is not None and ndx.options is not None
    assert ndx.price == pytest.approx(300.0, abs=0.05) and ndx.price_is_live      # from NDX option quotes
    assert ndx.prev_close is not None and ndx.source_note
    assert any("/v3/snapshot/options/I:NDX" in c for c in fake.calls)
    assert any("/aggs/ticker/QQQ/" in c for c in fake.calls)
    assert store.get("spot:NDX")["price"] == pytest.approx(300.0, abs=0.05)


def test_prev_close_skips_weekend_index_bar(cfg):
    sat = datetime(2026, 10, 10, 1, 0, tzinfo=ET)
    fri = datetime(2026, 10, 9, 1, 0, tzinfo=ET)
    bars = [{"t": int(fri.timestamp() * 1000), "o": 1, "h": 1, "l": 1, "c": 30883.15},
            {"t": int(sat.timestamp() * 1000), "o": 1, "h": 1, "l": 1, "c": 30853.32}]
    client = MassiveClient("test-key", transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"results": bars})))
    eng = Engine(cfg, client, Store(cfg.db_path), None)
    assert run(eng._prev_close("I:NDX", date(2026, 10, 12))) == 30883.15
    run(client.close())


def test_ndx_report_every_10_minutes(cfg):
    from mias.app import Service
    cfg = _ndx_cfg(cfg)
    t0 = datetime.combine(TODAY, time(10, 0), ET)
    fake = FakeMassive(TODAY, t0, {"AAA": 100.0, "QQQ": 750.0, "I:NDX": 300.0})
    svc = Service(cfg, transport=httpx.MockTransport(fake.handler))
    svc.client._retries = 0

    async def go():
        for minutes in (0, 2.5, 5, 7.5, 10, 12.5):        # 10:00 .. 10:12:30 -> slots 10:00 and 10:10
            await svc._tick(t0 + timedelta(minutes=minutes))
        await svc.client.close()
    run(go())
    reports = [a for a in svc.store.recent_alerts() if a["rule"] == "report"]
    assert len(reports) == 2 and all("NDX" in a["text"] and "10-min report" in a["text"] for a in reports)
    assert "since last" in reports[0]["text"]               # newest first: the 10:10 report compares to 10:00


# ---- daily spread picks --------------------------------------------------------------------------

def _leg(kind, strike, dte, delta, mid, oi=5000, spread=0.04):
    from mias.options import Contract
    exp = TODAY + timedelta(days=dte)
    return Contract(f"O:X{kind}{strike}{dte}", kind, strike, exp, dte, 100, mid, oi, 0.3, delta, 0.02,
                    mid - spread / 2, mid + spread / 2, 1, mid, 1)


def _chain_legs():
    legs = []
    for dte in (10, 44, 72):                        # only 44 is inside the 30-60 day window
        for k, cd, cm, pd_, pm in ((90, .80, 11.0, -.20, 1.2), (95, .65, 7.0, -.33, 2.2), (100, .50, 4.5, -.50, 4.4),
                                   (105, .33, 2.4, -.66, 7.2), (110, .22, 1.3, -.80, 11.1)):
            legs += [_leg("call", k, dte, cd, cm), _leg("put", k, dte, pd_, pm)]
    return legs


def test_build_spread_picks_window_deltas_and_liquidity():
    from mias.picks import build_spread
    long, short = build_spread(_chain_legs(), "call")
    assert (long.strike, short.strike, long.dte) == (100, 110, 44)
    long, short = build_spread(_chain_legs(), "put")
    assert (long.strike, short.strike, long.dte) == (100, 90, 44)
    thin = [c for c in _chain_legs() if c.strike != 110] + [_leg("call", 110, 44, .22, 1.3, oi=50)]
    assert build_spread(thin, "call")[1].strike == 105        # illiquid 110C skipped -> nearest liquid short
    assert build_spread([_leg("call", 100, 10, .5, 4.5)], "call") is None


def test_candidates_rules(cfg):
    from mias.picks import candidates
    up = _aligned(75, 40, 10)                          # strong up swing, options mildly bullish
    up.symbol = "UPS"
    against = _aligned(75, 40, -40); against.symbol = "AGN"   # options money against: excluded
    weak = _aligned(35, 30, 5); weak.symbol = "WEAK"           # tier B
    down = _aligned(-70, -65, -30); down.symbol = "DWN"
    calls = candidates([up, against, weak, down], "call", cfg)
    assert [(t, r.symbol) for t, _, r in calls] == [("A", "UPS"), ("B", "WEAK")]
    puts = candidates([up, against, weak, down], "put", cfg)
    assert [(t, r.symbol) for t, _, r in puts] == [("A", "DWN")]


def test_daily_picks_message_with_and_without_put(cfg, monkeypatch):
    from mias import picks
    from mias.formatting import picks_message
    up = _aligned(75, 40, 10); up.symbol = "UPS"; up.live_price = 100.0

    class FakeClient:
        async def option_chain(self, sym, **kw):
            return _chain_legs(), False
    monkeypatch.setattr(picks, "parse_contract", lambda item, today, tz: item)   # chain items are Contracts
    now = datetime.combine(TODAY, time(10, 0), ET)
    p = run(picks.daily_picks(FakeClient(), [up], cfg, now))
    assert p["call"].symbol == "UPS" and p["put"] is None and p["put_near"].symbol == "UPS"
    s = p["call"]
    assert s.cost == pytest.approx(3.2) and s.max_profit == pytest.approx(6.8) and s.breakeven == pytest.approx(103.2)
    text = picks_message(p, now)
    assert "CALL SPREAD" in text and "Buy" in text and "100C" in text and "110C" in text and "Limit <b>3.20</b>" in text
    assert "PUT SPREAD" in text and "No trade today" in text and "time stop" in text


def test_picks_sent_once_at_10(cfg, monkeypatch):
    from mias.app import Service
    t0 = datetime.combine(TODAY, time(9, 57, 30), ET)
    fake = FakeMassive(TODAY, t0, {"AAA": 100.0, "BBB": 200.0, "CCC": 50.0})
    svc = Service(cfg, transport=httpx.MockTransport(fake.handler))
    svc.client._retries = 0
    calls = []

    async def fake_text(now):
        calls.append(now)
        return "🎯 picks"
    svc.picks_text = fake_text

    async def go():
        for minutes in (0, 2.5, 5, 7.5):                     # 09:57:30 .. 10:05:00
            await svc._tick(t0 + timedelta(minutes=minutes))
        await svc.client.close()
    run(go())
    assert len(calls) == 1 and calls[0].astimezone(ET).time() >= time(10, 0)
    assert [a["rule"] for a in svc.store.recent_alerts()].count("picks") == 1


def test_temporary_report_stops_after_until_date(cfg):
    from mias.app import Service
    cfg = dataclasses.replace(cfg, reports={"AAA": 10, "BBB": 10}, report_until={"AAA": TODAY})
    t0 = datetime.combine(TODAY, time(11, 0), ET)
    fake = FakeMassive(TODAY, t0, {"AAA": 100.0, "BBB": 200.0, "CCC": 50.0})
    svc = Service(cfg, transport=httpx.MockTransport(fake.handler))
    svc.client._retries = 0
    tomorrow = datetime.combine(TODAY + timedelta(days=1), time(11, 0), ET)

    async def go():
        await svc._tick(t0)
        fake.today, fake.now = tomorrow.date(), tomorrow
        await svc._tick(tomorrow)
        await svc.client.close()
    run(go())
    sent = [(a["symbol"], a["ts"]) for a in svc.store.recent_alerts() if a["rule"] == "report"]
    assert sorted(s for s, _ in sent) == ["AAA", "BBB", "BBB"]      # AAA only on its last day; BBB both days


# ---- unusual activity report --------------------------------------------------------------------

def _with_unusual(sym, prints):
    from mias.options import OptionsSnapshot, Print
    snap = OptionsSnapshot(1, 100, 1, 1, 1, 1, 1, 1, 1.0, 1.0, 30, 30, 0, 0, None, None,
                           unusual=[Print(f"O:{sym}{i}", k, s, "2026-10-16", side, v, prem, oi, 1.0)
                                    for i, (k, s, side, v, prem, oi) in enumerate(prints)])
    return SymbolResult(sym, options=snap)


def test_unusual_report_ranks_marks_and_caps():
    from mias.formatting import unusual_report
    now = datetime.combine(TODAY, time(10, 40), ET)
    res = [_with_unusual("AAA", [("call", 100, "buy", 5000, 9e6, 100), ("put", 90, "buy", 4000, 2e6, 50),
                                 ("call", 105, "sell", 3000, 1.5e6, 10), ("call", 110, "buy", 2000, 1.2e6, 5)]),
           _with_unusual("BBB", [("put", 50, "sell", 8000, 5e6, 300)])]
    text, vols = unusual_report(res, now, None, top=10)
    lines = text.splitlines()
    assert "AAA 10/16 100C" in lines[2] and "BBB 10/16 50P" in lines[3]       # ranked by premium
    assert text.count("AAA ") == 3                                              # max 3 per symbol
    assert "🆕" not in text and "Lean" in text                                  # first report: no markers
    # Next report: AAA 100C grew, BBB unchanged, AAA 90P dropped from prev so it's "new" again.
    prev = dict(vols); prev["O:AAA0"] = 4000; prev.pop("O:AAA1")
    text2, _ = unusual_report(res, now, prev, top=10)
    assert "⬆️ 🟢 AAA 10/16 100C" in text2 and "+1,000 since last" in text2
    assert "🆕 🔴 AAA 10/16 90P" in text2 and "1 new, 1 growing" in text2
    assert unusual_report([SymbolResult("ZZZ")], now, None, 10)[0] is None     # nothing unusual: no message


def test_unusual_report_every_10_minutes(cfg):
    from mias.app import Service
    cfg = dataclasses.replace(cfg, unusual_every_minutes=10, reports={})
    t0 = datetime.combine(TODAY, time(10, 30), ET)
    fake = FakeMassive(TODAY, t0, {"AAA": 100.0, "BBB": 200.0, "CCC": 50.0})
    svc = Service(cfg, transport=httpx.MockTransport(fake.handler))
    svc.client._retries = 0
    sent = []
    import mias.formatting as fm
    real = fm.unusual_report

    def spy(results, now, prev, top, per_symbol=3):
        sent.append(now)
        return "🔥 report", {}
    fm.unusual_report = spy
    try:
        async def go():
            for minutes in (0, 2.5, 5, 7.5, 10, 12.5):        # slots 10:30 and 10:40
                await svc._tick(t0 + timedelta(minutes=minutes))
            await svc.client.close()
        run(go())
    finally:
        fm.unusual_report = real
    assert len(sent) == 2
    assert [a["rule"] for a in svc.store.recent_alerts()].count("unusual") == 2



# ---- sharing: group alerts, several users -------------------------------------------------------

def test_dispatcher_sends_to_every_chat_and_retries_only_missing(tmp_path):
    store = Store(tmp_path / "m.db")
    store.enqueue("t", "hello")
    sent, fail = [], {"g2": True}

    def handler(req):
        body = json.loads(req.content)
        if fail.get(body["chat_id"]):
            return httpx.Response(500, json={"description": "down"})
        sent.append(body["chat_id"])
        return httpx.Response(200, json={"ok": True})

    async def go():
        tg = Telegram("tok", "g1", transport=httpx.MockTransport(handler))
        d = Dispatcher(store, tg, ["g1", "g2"])
        assert await d.flush() == 0 and len(store.pending()) == 1      # g1 got it, g2 failed
        fail["g2"] = False
        assert await d.flush() == 1 and store.pending() == []
        await tg.close()
    run(go())
    assert sent == ["g1", "g2"]                                         # g1 never received it twice


def _shared_bot(cfg, monkeypatch):
    from mias.app import Service
    from mias.bot import CommandBot
    cfg = dataclasses.replace(cfg, telegram_token="tok", telegram_alert_chats=["-100"],
                              telegram_allowed_users=["1", "2", "3"], telegram_allowed_chats=["-100"])
    now = datetime.combine(TODAY, time(15, 0), ET)
    fake = FakeMassive(TODAY, now, {"AAA": 100.0, "BBB": 200.0, "CCC": 50.0})
    svc = Service(cfg, transport=httpx.MockTransport(fake.handler))
    svc.client._retries = 0
    bot = CommandBot(svc)
    out = []

    class FakeTG:
        async def send(self, html, chat=None):
            out.append((chat, html))

        async def close(self):
            pass
    svc.telegram = FakeTG()
    return svc, bot, out


def test_bot_users_group_and_whoami(cfg, monkeypatch):
    svc, bot, out = _shared_bot(cfg, monkeypatch)
    dm = lambda user, text: {"chat": {"id": int(user), "type": "private"}, "from": {"id": int(user)}, "text": text}
    grp = lambda user, text: {"chat": {"id": -100, "type": "supergroup"}, "from": {"id": int(user)}, "text": text}

    async def go():
        await bot._handle(dm("2", "/list"))              # friend in a private chat: reply goes to them
        await bot._handle(dm("9", "/list"))              # stranger: ignored
        await bot._handle(dm("9", "/whoami"))            # stranger can still learn their IDs
        await bot._handle(grp("3", "/list@NIAS_Bot"))    # command in the group: reply to the group
        await bot._handle(grp("3", "AMD looks weak today"))   # normal chat in the group: silent
        await bot._handle(grp("3", "/nonsense"))         # unknown command in the group: silent
        await bot._handle(dm("1", "hello there friend"))      # unknown in a DM: helpful reply
        await svc.client.close()
        await bot.close()
    run(go())
    chats = [c for c, _ in out]
    assert chats == ["2", "9", "-100", "1"]
    assert "Watching" in out[0][1] and "Watching" in out[2][1]
    assert "not allowed yet" in out[1][1] and "<code>9</code>" in out[1][1]
    assert "didn't get that" in out[3][1]


def test_ids_come_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "k")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    monkeypatch.setenv("TELEGRAM_ALERT_CHATS", "-100123")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "111, 222,333")
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHATS", raising=False)
    cfg_path = tmp_path / "config.toml"           # away from the real .env next to the repo's config
    cfg_path.write_text((ROOT / "config.toml").read_text())
    c = config_mod.load(cfg_path)
    assert c.telegram_alert_chats == ["-100123"] and c.telegram_allowed_users == ["111", "222", "333"]
    assert c.telegram_allowed_chats == ["-100123"]          # defaults to the alert chats
    monkeypatch.delenv("TELEGRAM_ALERT_CHATS"); monkeypatch.delenv("TELEGRAM_ALLOWED_USERS")
    c = config_mod.load(cfg_path)
    assert c.telegram_alert_chats == ["111"] and c.telegram_allowed_users == ["111"]   # single-user default
