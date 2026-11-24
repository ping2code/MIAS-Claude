"""V3 EARLY WATCH (2026-11-18): config copied from V2, detection identical to V2, presentation (three-state checks,
margins, invalidation, options line, quality, flags, footer, no contract text), separate storage, v2_would_send,
reports, exception isolation, Telegram sending / own queue, kill switch, and V1 / V2 untouched."""
import asyncio
import hashlib
import re
import sqlite3
import subprocess
from dataclasses import replace
from datetime import date, datetime, time
from pathlib import Path
from types import SimpleNamespace

import pytest

from mias.early_watch_v3 import config as C
from mias.early_watch_v3 import present as Pr
from mias.early_watch_v3 import service as Svc
from mias.early_watch_v3 import tg
from mias.early_watch_v3.atr import atr_5m
from mias.early_watch_v3.detector import V3Detector
from mias.early_watch_v3.outcomes import outcomes
from mias.early_watch_v3.policy import V3Telegram
from mias.early_watch_v3.report import build, rate
from mias.early_watch_v3.store import V3DB
from mias.indicators import Bar, atr
from mias.observer import optctx, rules as obs_rules, telegram as obs_tg, v2 as obs_v2
from mias.observer.log import ObserverDB
from mias.observer.replay import ordered
from mias.observer.service import SEND_TIMEOUT_S
from mias.observer.settings import load as obs_load
from mias.observer.v2_replay import build_v2
from observer_synth import ET, daily, history, trading_days

ROOT = Path(__file__).resolve().parent.parent
V3TOML = ROOT / "early_watch_v3.toml"
DAYS = trading_days(date(2026, 11, 13), 12)
TODAY = DAYS[-1]
SYMS = ["SPY", "AAA", "BBB"]


# ---- 1. config copied from V2 ---------------------------------------------------------------------------

def test_config_values_are_a_copy_of_current_v2():
    v3 = C.load(V3TOML)
    obs = obs_load(ROOT / "config.toml")
    assert list(v3.symbols) == obs.symbols
    R = obs_rules.RULES
    for f in ("pivot_k", "dev_atr_mult", "atr_period", "ema_fast", "ema_slow", "session_open", "session_close",
              "or_end", "main_start", "main_end", "warm_sessions", "stale_bars_after_gap"):
        assert getattr(v3.rules, f) == getattr(R, f), f
    assert v3.rules.need == obs_v2.NEED and v3.rules.watch_minutes == obs_v2.WATCH_MINUTES
    assert v3.rules.vwap_prior_closes == 3 and v3.rules.vwap_invalid_closes == 2          # literals in v2.py
    pol = obs_v2.V2Telegram(max_age_s=obs.telegram_max_age_s)
    assert v3.tg.combine_s * 1000 == pol.combine_ms and v3.tg.max_age_s * 1000 == pol.max_age_ms
    assert (v3.tg.group_gap_s, v3.tg.private_gap_s, v3.tg.max_wait_s) == (obs_tg.GROUP_GAP_S, obs_tg.PRIVATE_GAP_S,
                                                                         obs_tg.MAX_WAIT_S)
    assert v3.tg.send_timeout_s == SEND_TIMEOUT_S and v3.options_fresh_s == optctx.FRESH_S
    assert v3.enabled and v3.telegram_enabled and v3.chat_id == "-5328364952"
    assert v3.tg.send == {"structure": True, "vwap": True, "or": True}        # launch: all three families send
    p = v3.pres
    assert (p.marginal_atr, p.tight_invalidation_atr, p.low_conviction_rvol, p.late_session_et) == \
        (0.10, 0.30, 0.8, time(14, 30))
    assert (p.inval_at_entry_atr, p.market_mismatch_min_spy_pct) == (0.05, 0.50)
    assert v3.db_path != obs.db_path and v3.db_path != obs.v2_db_path and "early_watch_v3" in str(v3.db_path)


# ---- 2. detection identical to V2 ------------------------------------------------------------------------

def _norm(evs):
    out = []
    for e in evs:
        k = e["kind"].split("_", 1)[1]
        if k == "candidate":
            out.append((k, e["cand_id"], e["status"], e["candidate_at"], e["side"], e["candidate_price"]))
        elif k == "candidate_end":
            out.append((k, e["cand_id"], e["status"], e["end_at"]))
        elif k == "watch":
            out.append((k, e["watch_id"], e["watch_at"], e["side"], e["family"], e["lane"], e["level"],
                        e["watch_price"], e["pre_ema"]))
        elif k == "end":
            out.append((k, e["watch_id"], e["state"], e["end_at"], e["end_reason"]))
        elif k == "ema_cross":
            out.append((k, e["sym"], e["side"], e["at"]))
    return out


@pytest.mark.parametrize("seed", [3, 7, 11, 19])
def test_v3_detector_equals_v2_on_identical_bars(seed):
    hist = history(SYMS, DAYS, seed=seed)
    dly = daily(hist)
    confirms = [{"sym": "AAA", "side": "bull", "ts": datetime.combine(TODAY, time(11, 7), ET).timestamp(),
                 "source": "swing_trend"}]
    v2 = build_v2(SYMS, TODAY, ET, hist, dly, confirms)
    v3 = Svc.build_detector(C.load(V3TOML), SYMS, TODAY, ET, hist, dly, confirms)
    e2, e3 = [], []
    for s, b in ordered(SYMS, {s: hist[s][TODAY] for s in SYMS}):
        a, c = v2.process(s, b), v3.process(s, b)
        e2 += a
        e3 += c
        for x, y in zip(a, c):                          # V3 context is V2's plus additions, same values
            if x["kind"] == "v2_watch":
                assert {k: v for k, v in y["context"].items() if k in x["context"]} == x["context"]
    e2 += v2.finalize()
    e3 += v3.finalize()
    assert _norm(e2) == _norm(e3)
    assert sum(e["kind"] == "v3_watch" for e in e3) > 0


@pytest.mark.skipif(not (ROOT / "data/observer/v2.db").exists() or not (ROOT / "data/observer/cache").exists(),
                    reason="live V2 data not on this machine")
def test_v3_replay_of_a_live_session_matches_v2_live_records(tmp_path):
    from mias import config as mc
    from mias.early_watch_v3.replay import replay_day
    cfg = mc.load(ROOT / "config.toml", require_secrets=False)
    v3 = replace(C.load(V3TOML), bars_db=ROOT / "data/observer/observer.db", v2_db=ROOT / "data/observer/v2.db",
                 source_cache_dir=ROOT / "data/observer/cache", cache_dir=tmp_path / "cache")
    day = date(2026, 11, 18)
    out = asyncio.run(replay_day(cfg, v3, day, tmp_path / "r.db", client=None, options=False))
    assert "detection matches → **MATCH**" in out["report"]
    v2 = sqlite3.connect(f"file:{ROOT / 'data/observer/v2.db'}?mode=ro", uri=True)
    a = sorted(v2.execute("SELECT watch_id, watch_at, side, state, end_reason FROM v2_watches WHERE day=?",
                          (str(day),)))
    b = sorted(sqlite3.connect(tmp_path / "r.db").execute(
        "SELECT watch_id, watch_at, side, state, end_reason FROM v3_watches WHERE day=?", (str(day),)))
    assert a == b and len(a) > 100


def test_atr_is_one_definition_equal_to_v2s():
    hist = history(["AAA"], DAYS, seed=5)
    from mias.observer.state import SymbolState as S2, five_minute
    from mias.early_watch_v3.state import SymbolState as S3
    a, b = S2("AAA", ET), S3("AAA", ET)
    prior = [(d, bs) for d, bs in hist["AAA"].items() if d < TODAY]
    a.warm(prior)
    b.warm(prior)
    a.start_day(TODAY, None)
    b.start_day(TODAY, None)
    for bar in hist["AAA"][TODAY]:
        fa, fb = a.on_minute(bar), b.on_minute(bar)
        assert fa.atr == fb.atr
    bars5 = five_minute([x for _, bs in prior for x in bs])
    assert atr_5m(bars5, 14) == atr(bars5[-300:], 14) and atr_5m(bars5) is not None


# ---- 3. presentation ------------------------------------------------------------------------------------

PRES = C.Presentation()
CLOSE = int(datetime.combine(TODAY, time(16, 0), ET).timestamp() * 1000)


def optx(view="NEUTRAL", market=None, stock_rel=None, overall=None, status="ok", age=47.0):
    rel = {s: stock_rel or Pr.relation(view, s) for s in ("bull", "bear")}
    return {"status": status, "view": view, "market_view": market, "relation": rel, "age_s": age,
            "snapshot_ts": 1.0, "overall": {s: (overall or {}).get(s, rel[s]) for s in ("bull", "bear")}}


def W(side="bull", family="vwap", price=100.0, level=None, hhmm=(11, 0), **ctx):
    c = {"vwap_exact": 99.8, "atr14_5m": 0.5, "sma200": 90.0, "spy_pct": 0.4, "rel_vol": 1.2, "or_high": 99.0,
         "or_low": 97.0, "optx": optx()}
    c.update(ctx)
    at_ms = int(datetime.combine(TODAY, time(*hhmm), ET).timestamp() * 1000)
    return {"watch_id": "x", "day": str(TODAY), "family": family, "sym": "AAA", "side": side, "lane": "main",
            "leg_id": "L", "watch_at": at_ms, "watch_price": price, "level": level if level is not None else 99.8,
            "context": c, "ema_watch": {"ema9_c": 100.1, "ema21_c": 99.9,
                                        "crossed_provisional": side == "bull"}}     # side-relative, as the detector


def view(w, fams=(), prog=None, **kw):
    return Pr.build_view(w, list(fams), prog, kw.pop("pres", PRES), ET, CLOSE, 330, **kw)


def test_three_state_checks_bull_and_bear():
    v = view(W("bull", family="structure", level=99.5))
    assert {k: c["state"] for k, c in v["checks"].items()} == {
        "vwap": "support", "ema": "support", "sma200": "support", "spy": "support", "volume": "support",
        "options": "neutral"}
    b = view(W("bear", family="structure", level=100.5))
    assert {k: c["state"] for k, c in b["checks"].items()} == {
        "vwap": "against", "ema": "against", "sma200": "against", "spy": "against", "volume": "support",
        "options": "neutral"}
    text = Pr.render(b)
    assert "✗ Above VWAP $99.80" in text and "✗ EMA9 above EMA21 (5m)" in text and "✗ SPY +0.40%" in text
    bull_against = Pr.render(view(W("bull", family="structure", level=99.0, price=99.5, spy_pct=-1.24)))
    assert "✗ Below VWAP $99.80 −0.30%" in bull_against and "✗ SPY −1.24%" in bull_against
    bear_ok = Pr.render(view(W("bear", family="structure", level=100.0, price=99.5, spy_pct=-1.24)))
    assert "✓ Below VWAP $99.80 −0.30% · −0.60 ATR" in bear_ok and "✓ SPY −1.24%" in bear_ok


def test_vwap_wording_follows_detector_state():
    # confirmed V3-B Watch: ✓, never the V2 "○ Above VWAP" (the TSLA 2026-11-17 14:35 case: $379.96 vs $379.78)
    t = Pr.render(view(W("bull", "vwap", price=379.96, vwap_exact=379.78, atr14_5m=0.94), fams=["vwap"]))
    assert "✓ Above VWAP $379.78 +0.05% · +0.19 ATR" in t and "○ Above VWAP" not in t
    # partly met: a pending reclaim with 1 of 2 hold closes
    t = Pr.render(view(W("bull", "structure", level=99.5), prog=(1, 2)))
    assert "○ VWAP reclaim developing — 1/2 closes above" in t
    t = Pr.render(view(W("bear", "structure", level=100.5, price=99.7), prog=(1, 2)))
    assert "○ VWAP loss developing — 1/2 closes below" in t
    # marginal: under 0.10 ATR
    t = Pr.render(view(W("bull", "vwap", price=99.84, vwap_exact=99.8, atr14_5m=0.5), fams=["vwap"]))
    assert "✓ Above VWAP $99.80 +0.04% · +0.08 ATR (marginal)" in t
    assert "VWAP n/a" in Pr.render(view(W(vwap_exact=None, vwap=None)))


def test_ema_sma200_spy_volume_checks():
    w = W()
    w["ema_watch"] = {"ema9_c": 99.0, "ema21_c": 99.5, "crossed_provisional": True}
    assert view(w)["checks"]["ema"] == {"state": "neutral", "text": "EMA9/21 (5m) cross up developing"}
    w["ema_watch"]["crossed_provisional"] = False
    assert view(w)["checks"]["ema"]["state"] == "against"
    w["ema_watch"] = {"ema9_c": None, "ema21_c": None}
    assert view(w)["checks"]["ema"]["state"] == "neutral"
    assert view(W(sma200=None))["checks"]["sma200"]["state"] == "neutral"
    assert view(W(sma200=110.0))["checks"]["sma200"] == {"state": "against", "text": "Below SMA200 $110.00 (−9.1%)"}
    s = W()
    s["sym"] = "SPY"
    assert view(s)["checks"]["spy"]["state"] == "neutral"
    assert view(W(spy_pct=None))["checks"]["spy"]["state"] == "neutral"
    assert view(W(rel_vol=0.9))["checks"]["volume"]["state"] == "neutral"
    assert view(W(rel_vol=0.5))["checks"]["volume"] == {"state": "against", "text": "Volume 0.5× normal (low)"}
    assert view(W(rel_vol=None))["checks"]["volume"]["state"] == "neutral"


def test_options_line_overall_consistency_mismatch_stale_unavailable():
    x = optx("NEUTRAL", market="BULLISH_CONFIRMATION", overall={"bull": "CONFIRMING", "bear": "CONFLICTING"})
    v = view(W("bull", spy_pct=-1.2, optx=x))
    t = Pr.render(v)
    assert "Stock options: NEUTRAL | Market options: CONFIRMING | Overall: CONFIRMING" in t
    assert "✓ Options: CONFIRMING" in t and v["checks"]["options"]["state"] == "support"
    assert "Market mismatch: options bullish while SPY −1.20%" in t and "Options age: 47s" in t
    tb = Pr.render(view(W("bear", spy_pct=-1.2, optx=x)))
    assert "Overall: CONFLICTING" in tb and "✗ Options: CONFLICTING" in tb
    for side in ("bull", "bear"):                       # the headline verdict always equals Overall
        for ov in ("CONFIRMING", "CONFLICTING", "NEUTRAL", "MIXED", "INSUFFICIENT_DATA"):
            vv = view(W(side, optx=optx(overall={side: ov})))
            word = Pr.WORD[ov]
            tt = Pr.render(vv)
            assert f"Options: {word}" in tt and tt.count(f"Overall: {word}") == 1
    st = Pr.render(view(W(optx=optx(status="stale", age=412.0, view="INSUFFICIENT_DATA"))))
    assert "Options: STALE (age 412s)" in st and "Overall: INSUFFICIENT DATA" in st
    assert "Options: STALE" in Pr.render(view(W(optx=optx(age=400.0))))      # past the existing freshness limit
    un = Pr.render(view(W(optx=None)))
    assert "Options: unavailable" in un and "○ Options: INSUFFICIENT DATA" in un
    idx = W(optx=optx())
    idx["sym"] = "QQQ"
    assert "Market options: n/a (index)" in Pr.render(view(idx))


def test_quality_counts_low_conviction_late_session_minutes_to_close():
    v = view(W("bull", family="structure", level=99.5, rel_vol=0.7, hhmm=(14, 35)))
    marks = [v["checks"][k]["state"] for k in Pr.CHECKS]
    assert (v["n_support"], v["n_against"], v["n_neutral"]) == (marks.count("support"), marks.count("against"),
                                                                  marks.count("neutral"))
    assert sum((v["n_support"], v["n_against"], v["n_neutral"])) == 6
    t = Pr.render(v)
    assert f"Quality: Supports {v['n_support']} · Against {v['n_against']} · Neutral {v['n_neutral']}" in t
    assert t.split("\n")[0].endswith("· low conviction") and v["low_conviction"]
    assert "Minutes to close: 85 · late session" in t and v["late_session"]
    early = view(W(hhmm=(14, 29), rel_vol=0.8))
    assert not early["late_session"] and not early["low_conviction"] and early["minutes_to_close"] == 91
    assert "late session" not in Pr.render(early) and "low conviction" not in Pr.render(early)


def test_invalidation_distance_tight_label_and_trigger_wording():
    v = view(W("bull", "structure", price=100.0, level=99.6, atr14_5m=0.55))
    t = Pr.render(v)
    assert "Invalidation: $99.60 · +0.40% · 0.73 ATR" in t and "tight" not in t and not v["tight"]
    assert "Trigger: developing HL at $99.60 (V3-A Structure)" in t
    tight = Pr.render(view(W("bear", "structure", price=100.0, level=100.1, atr14_5m=0.5)))
    assert "Invalidation: $100.10 · +0.10% · 0.20 ATR · tight — noise risk" in tight
    assert "developing LH" in tight and "developing HL" not in tight
    vw = Pr.render(view(W("bear", "vwap", price=99.5, vwap_exact=99.8), fams=["vwap"]))
    assert "Trigger: VWAP loss — held below (V3-B VWAP)" in vw and "reclaim" not in vw
    assert "Invalidation: 2 closes back above VWAP (now $99.80 · +0.30% · 0.60 ATR)" in vw
    orb = Pr.render(view(W("bull", "or", price=99.5, level=99.0), fams=["or"]))
    assert "Trigger: OR breakout above $99.00 +0.51% · +1.00 ATR (V3-C OR)" in orb and "breakdown" not in orb
    ord_ = Pr.render(view(W("bear", "or", price=96.5, level=97.0), fams=["or"]))
    assert "OR breakdown below $97.00" in ord_ and "breakout" not in ord_


def test_zero_distance_invalidation_shows_at_entry():
    # PG 2026-11-19 11:36 ET: the confirming close equals the frozen LH level (V2 has the same level)
    v = view(W("bear", "structure", price=168.27, level=168.27, atr14_5m=0.3911))
    t = Pr.render(v)
    assert "Invalidation: $168.27 · at entry (0 ATR) · tight — noise risk" in t
    assert v["invalidation_at_entry"] and v["tight"] and v["inval_level"] == 168.27
    near = view(W("bull", "structure", price=100.0, level=99.98, atr14_5m=0.5))      # 0.04 ATR
    assert near["invalidation_at_entry"] and "at entry (0 ATR)" in Pr.render(near)
    far = view(W("bull", "structure", price=100.0, level=99.97, atr14_5m=0.5))       # 0.06 ATR
    assert not far["invalidation_at_entry"] and "Invalidation: $99.97 · +0.03% · 0.06 ATR" in Pr.render(far)
    vw = view(W("bull", "vwap", price=99.81, vwap_exact=99.8), fams=["vwap"])
    assert vw["invalidation_at_entry"] and "(now $99.80 · at entry (0 ATR))" in Pr.render(vw)


def test_number_format_one_minus_sign_and_no_negative_zero():
    assert Pr._num(-0.0, "+.2f") == "0.00" and Pr._num(-0.004, "+.2f") == "0.00" and Pr._num(0.0, "+.2f") == "0.00"
    assert Pr._num(-0.004, ".2f") == "0.00" and Pr._num(-1.24, "+.2f") == "−1.24" and Pr._num(0.4, "+.2f") == "+0.40"
    texts = _many_texts() + [Pr.render(view(W(s, f, price=p, level=p, vwap_exact=p, spy_pct=sp, atr14_5m=0.39),
                                            fams=[f]))
                             for s in ("bull", "bear") for f in ("structure", "vwap", "or")
                             for p in (168.27, 99.8) for sp in (-0.0, -0.001, 0.0, -1.24)]
    for t in texts:
        assert "-0.00" not in t and "−0.00" not in t and "+0.00" not in t
        assert not re.search(r"(?<![A-Za-z0-9])-\d", t), t            # never an ASCII hyphen-minus on a number


def test_market_mismatch_line_needs_a_half_percent_spy_move():
    x = optx("NEUTRAL", market="BULLISH_CONFIRMATION")
    small = view(W("bear", spy_pct=-0.18, optx=x))
    assert small["market_mismatch"] == "Market mismatch: options bullish while SPY −0.18%"    # still stored
    assert not small["mismatch_shown"] and "Market mismatch" not in Pr.render(small)
    big = view(W("bear", spy_pct=-1.24, optx=x))
    assert big["mismatch_shown"] and "Market mismatch: options bullish while SPY −1.24%" in Pr.render(big)
    edge = view(W("bear", spy_pct=-0.50, optx=x))
    assert edge["mismatch_shown"]
    # display only: options verdicts and the quality counts do not depend on whether the line is shown
    for k in ("options_stock", "options_market", "options_overall", "n_support", "n_against", "n_neutral",
              "checks"):
        assert small[k] == view(W("bear", spy_pct=-0.18, optx=x), pres=C.Presentation(
            market_mismatch_min_spy_pct=0.0))[k]


def test_presentation_flags_are_stored_and_backfilled_on_old_databases(tmp_path):
    import sqlite3 as sq
    from mias.early_watch_v3.store import V3DB, SCHEMA
    old = tmp_path / "old.db"
    c = sq.connect(old)
    c.executescript(SCHEMA.replace(" mismatch_shown INTEGER, invalidation_at_entry INTEGER,", ""))
    c.execute("INSERT INTO v3_watches (watch_id, inval_dist_atr, market_mismatch) VALUES ('a', -0.0, 'm')")
    c.execute("INSERT INTO v3_watches (watch_id, inval_dist_atr) VALUES ('b', 0.48)")
    c.commit(); c.close()
    db = V3DB(old)
    got = {w["watch_id"]: (w["invalidation_at_entry"], w["mismatch_shown"]) for w in db.watches()}
    assert got == {"a": (1, 1), "b": (0, 0)}
    db2 = V3DB(old)                                     # idempotent re-open
    db2.close()
    v = view(W("bear", "structure", price=168.27, level=168.27, spy_pct=-0.18,
               optx=optx("NEUTRAL", market="BULLISH_CONFIRMATION")))
    db.db.execute("INSERT INTO v3_watches (watch_id) VALUES ('c')")
    db.set_view("c", v, Pr.render(v))
    w = db.watches("watch_id='c'")[0]
    assert w["invalidation_at_entry"] == 1 and w["mismatch_shown"] == 0 and w["market_mismatch"]
    db.close()


def test_header_family_tags_and_combined():
    t = Pr.render(view(W("bear", "structure", level=100.5, price=99.7, hhmm=(14, 47)), fams=["structure", "vwap", "or"]))
    head = t.split("\n")[0]
    assert head == "🧭 V3 EARLY WATCH · V3-A Structure + V3-B VWAP + V3-C OR · AAA · BEARISH · 14:47 ET @ $99.70"
    ents = obs_tg.heading_entities(t)                 # the current bold heading style
    assert ents and head[2:2 + ents[0]["length"]].endswith("BEARISH")


FORBIDDEN = re.compile(r"strike|expir|contract|\bCALLS?\b|\bPUTS?\b|target|stop|profit|\bbuy\b|\bsell\b|DTE",
                       re.IGNORECASE)


def _many_texts():
    out = []
    for side in ("bull", "bear"):
        for fam in ("structure", "vwap", "or"):
            for x in (optx(), None, optx(status="stale", age=500.0), optx(market="BEARISH_CONFIRMATION")):
                out.append(Pr.render(view(W(side, fam, optx=x, rel_vol=0.5, hhmm=(14, 50)), fams=[fam, "vwap"])))
    return out


def test_footer_is_exact_with_nothing_appended():
    for t in _many_texts():
        assert t.endswith(Pr.FOOTER) and t.split("\n")[-1] == "WATCH ONLY — NOT A TRADE SIGNAL"
        assert t.count("NOT A TRADE SIGNAL") == 1 and not re.search(r"SIGNAL\S", t)


def test_no_contract_recommendation_text():
    for t in _many_texts():
        assert not FORBIDDEN.search(t.replace("Stock options", "").replace("Market options", "")), t


# ---- 4. live runner on a synthetic session ---------------------------------------------------------------

class FakeSender:
    def __init__(self, fail=False):
        self.texts, self.fail, self.rate_limited = [], fail, 0

    async def send(self, text):
        if self.fail:
            raise obs_tg.SendError("HTTP 400: chat not found")
        self.texts.append(text)
        return [str(len(self.texts))]


def _cfg():
    return SimpleNamespace(tz=ET, db_path=Path("/nonexistent"))


def _setup(tmp_path, seed=7, toml_edits=(), sender=None, options_factory=None):
    hist = history(SYMS, DAYS, seed=seed)
    src = ObserverDB(tmp_path / "observer.db")
    for s, b in ordered(SYMS, {s: hist[s][TODAY] for s in SYMS}):
        src.bar1(str(TODAY), s, b, "ws")
        src.ws_bar({"sym": s, "s": b.t, "e": b.t + 60_000, "o": b.o, "h": b.h, "l": b.l, "c": b.c, "v": b.v},
                   (b.t + 60_000) / 1000 + 0.4, "ok", 1)
    src.close()
    text = V3TOML.read_text()
    for a, b in toml_edits:
        text = text.replace(a, b)
    p = tmp_path / "v3.toml"
    p.write_text(text)
    v3 = replace(C.load(p), symbols=tuple(SYMS), bars_db=tmp_path / "observer.db", db_path=tmp_path / "v3" / "v3.db",
                 v2_db=None, cache_dir=tmp_path / "cache", source_cache_dir=None)
    sim = {"t": datetime.combine(TODAY, time(9, 25), ET).timestamp()}

    async def warm(day):
        return hist, daily(hist), {}
    store = V3DB(v3.db_path)
    r = Svc.V3Runner(_cfg(), v3, store, clock=lambda: sim["t"], warm=warm, sender=sender,
                     options_factory=options_factory, config_toml=str(ROOT / "config.toml"))
    asyncio.run(r.start_day(TODAY, []))
    return r, store, sim, p


def _run_all(r, sim):
    conn = Svc.ro_connect(r.v3.bars_db)
    ends = [s + 60_000 for (s,) in conn.execute("SELECT s FROM bars1 ORDER BY rowid")]
    conn.close()
    for e in ends:
        sim["t"] = e / 1000 + 1
        r.tail(limit=1)


def _drain(r):
    async def go():
        while not r.outbox.empty():
            await r.send_one(*r.outbox.get_nowait())
    asyncio.run(go())


def test_runner_stores_every_watch_sends_all_three_families_and_tags_v2_would_send(tmp_path):
    snd = FakeSender()                                      # launch policy (min_families = 1): every family sends
    r, store, sim, _ = _setup(tmp_path, sender=snd, toml_edits=[("min_families = 2 ", "min_families = 1 ")])
    _run_all(r, sim)
    _drain(r)
    ws = store.watches()
    assert ws and r.counts["errors_detector"] == r.counts["errors_store"] == 0
    sent = [w for w in ws if w["telegram_sent"]]
    assert sent and len(snd.texts) == len(sent)
    assert {w["family"] for w in ws if w["v3_verdict"] == "send"} == {"structure", "vwap", "or"}
    for w in ws:
        assert w["text"] and w["view"] and w["chk_vwap"] in ("support", "against", "neutral")
        assert w["n_support"] + w["n_against"] + w["n_neutral"] == 6 and w["atr"]
        assert w["bar_received_at"] is not None and w["created_at"] is not None
        if w["family"] == "structure":                          # V2 mutes Structure alone
            assert not w["v2_would_send"]
        if w["v3_verdict"] == "send":
            assert w["telegram_queued_at"] is not None
    assert any(w["v2_would_send"] for w in ws if w["family"] != "structure")
    for w in sent:
        assert w["telegram_text"] == w["text"] and w["telegram_sent_at"] >= w["telegram_queued_at"]
    life = store.db.execute("SELECT COUNT(*) FROM v3_lifecycle WHERE event='WATCH'").fetchone()[0]
    assert life == len(ws)


def test_outcomes_and_report(tmp_path):
    r, store, sim, _ = _setup(tmp_path)
    _run_all(r, sim)
    sim["t"] = datetime.combine(TODAY, time(16, 5), ET).timestamp()
    text = r.end_of_day()
    assert text and "## Totals" in text and "## V2 vs V3" in text and "## Latency" in text
    for g in ("family", "Supports count", "low conviction", "late session", "tight invalidation", "options Overall",
              "index vs single stock", "V2 policy"):
        assert f"**By {g}**" in text
    assert "sample too small" in text
    w = store.watches("state != 'WATCH'")[0]
    assert w["outcomes_at"] is not None and (Path(store.path).parent / "daily" / str(TODAY) / "v3_report.md").exists()
    assert rate(10, 29) == "10/29 (sample too small)" and rate(15, 30) == "50% (15/30)"


def test_outcome_math():
    t0 = 1_000 * 60_000
    w = {"side": "bear", "watch_price": 100.0, "watch_at": t0, "atr": 0.5}
    bars = [(t0 + i * 60_000, 0, 100.2, 99.0 - 0.1 * i, 99.5, 1) for i in range(70)]
    o = outcomes(w, bars, t0 + 600 * 60_000)
    assert o["out_5"] == pytest.approx(0.5) and o["mae_pct"] == pytest.approx(-0.2)
    assert o["mfe_pct"] == pytest.approx(100 * (100 - (99.0 - 0.1 * 59)) / 100, rel=1e-3)
    assert outcomes(w, bars, t0 + 10 * 60_000)["out_15"] is None              # past the session close


# ---- 5. isolation ---------------------------------------------------------------------------------------

def _hash(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def test_v3_never_writes_the_v1_v2_source_and_its_connection_is_read_only(tmp_path):
    r, store, sim, _ = _setup(tmp_path)
    before = _hash(tmp_path / "observer.db")
    _run_all(r, sim)
    with pytest.raises(sqlite3.OperationalError):
        r._bars_conn().execute("INSERT INTO feed_events VALUES (1, 'x', 'y')")
    assert _hash(tmp_path / "observer.db") == before
    assert Path(store.path) != tmp_path / "observer.db"
    assert {t for (t,) in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")} >= {
        "v3_watches", "v3_bars", "v3_lifecycle", "v3_candidate_log"}


def test_detector_storage_render_and_options_exceptions_stay_in_v3(tmp_path, monkeypatch):
    def boom_ctx():
        def ctx(sym, ts):
            raise RuntimeError("options down")
        return ctx
    r, store, sim, _ = _setup(tmp_path, options_factory=boom_ctx)
    real = r.det.process
    calls = {"n": 0}

    def flaky(sym, b, gap=False, rec=None):
        calls["n"] += 1
        if calls["n"] in (5, 6):
            raise ValueError("detector bug")
        return real(sym, b, gap, rec)
    monkeypatch.setattr(r.det, "process", flaky)
    real_apply = store.apply
    monkeypatch.setattr(store, "apply", lambda ev, mode, meta=None: (_ for _ in ()).throw(sqlite3.OperationalError(
        "disk")) if calls["n"] == 9 else real_apply(ev, mode, meta))
    real_render = Svc.render
    seen = {"n": 0}

    def bad_render(v):
        seen["n"] += 1
        if seen["n"] == 1:
            raise KeyError("render bug")
        return real_render(v)
    monkeypatch.setattr(Svc, "render", bad_render)
    _run_all(r, sim)
    assert r.counts["errors_detector"] == 2 and r.counts["errors_store"] == 1 and r.counts["errors_present"] == 1
    assert r.counts["bars"] > 1000
    ws = store.watches()
    assert ws and sum(bool(w["render_error"]) for w in ws) == 1
    bad = next(w for w in ws if w["render_error"])
    assert not bad["telegram_sent"] and bad["telegram_queued_at"] is None
    good = [w for w in ws if w["view"]]
    assert all(w["options_status"] == "unavailable" and "Options: unavailable" in w["text"] for w in good)
    assert all("context_error" in w["context"] for w in good)


def test_missing_options_context_entirely(tmp_path):
    r, store, sim, _ = _setup(tmp_path, options_factory=lambda: (_ for _ in ()).throw(OSError("no MIAS db")))
    assert r.octx is None
    _run_all(r, sim)
    assert store.watches() and all("Options: unavailable" in w["text"] for w in store.watches())


def test_telegram_failure_is_recorded_and_changes_nothing_else(tmp_path):
    r, store, sim, _ = _setup(tmp_path, sender=FakeSender(fail=True))
    _run_all(r, sim)
    n_before = len(store.watches())
    _drain(r)
    ws = store.watches()
    failed = [w for w in ws if w["v3_verdict"] == "send"]
    assert failed and all(not w["telegram_sent"] and "chat not found" in w["telegram_error"] for w in failed)
    assert len(ws) == n_before and r.counts["tg_failed"] == len(failed)
    assert all(w["text"] and w["view"] for w in ws)


V1V2_TOKEN = "111111:V1V2-BOT-TOKEN-must-never-be-used"
V3_TOKEN = "222222:V3-BOT-TOKEN-secret"


def test_v3_uses_its_own_token_and_chat_and_own_rate_limit_queue(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", V1V2_TOKEN)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_V3", V3_TOKEN)
    v3 = C.load(V3TOML)
    s1, why = tg.make_sender(v3)
    s2, _ = tg.make_sender(v3)
    try:
        assert why is None and s1.chats == ["-5328364952"]
        assert V3_TOKEN in str(s1._url) and V1V2_TOKEN not in str(s1._url)
        assert s1 is not s2 and s1._next is not s2._next and s1._lock is not s2._lock   # own pacing state
        assert s1._gap("-5328364952") == v3.tg.group_gap_s and s1._gap("123") == v3.tg.private_gap_s
        assert s1.max_wait_s == v3.tg.max_wait_s
    finally:
        asyncio.run(s1.close())
        asyncio.run(s2.close())


def test_no_fallback_to_the_v1_v2_token_or_chats(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", V1V2_TOKEN)
    monkeypatch.setenv("TELEGRAM_ALERT_CHATS", "-100,-5323269951")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN_V3", raising=False)
    assert tg.make_sender(C.load(V3TOML)) == (None, "send skipped: no V3 token")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_V3", V3_TOKEN)
    assert tg.make_sender(replace(C.load(V3TOML), chat_id=None)) == (None, "send skipped: no V3 chat")
    import inspect
    from mias.early_watch_v3 import service
    for mod in (tg, service):                           # nothing in V3 reads the V1 / V2 token or alert chats
        src = inspect.getsource(mod)
        assert "telegram_token" not in src and "telegram_alert_chats" not in src
        assert not re.search(r"TELEGRAM_BOT_TOKEN(?!_V3)", src)


def test_secrets_file_loads_only_the_v3_token(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN_V3", raising=False)
    monkeypatch.delenv("SOMETHING_ELSE", raising=False)
    f = tmp_path / ".env.v3"
    f.write_text(f"# V3 only\nexport TELEGRAM_BOT_TOKEN_V3='{V3_TOKEN}'\nSOMETHING_ELSE=x\n")
    tg.load_secrets(f)
    import os
    assert tg.token() == V3_TOKEN and "SOMETHING_ELSE" not in os.environ
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_V3", "from-the-service-env")
    tg.load_secrets(f)
    assert tg.token() == "from-the-service-env"         # the real environment wins


def test_missing_token_means_detect_store_and_log_only(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN_V3", raising=False)
    sender, why = tg.make_sender(C.load(V3TOML))
    assert sender is None
    r, store, sim, _ = _setup(tmp_path, sender=None)
    r.no_sender_reason = why
    _run_all(r, sim)
    ws = store.watches()
    would = [w for w in ws if w["v3_verdict"] == "send"]
    assert ws and would and r.outbox.empty() and r.counts["tg_no_token"] == len(would)
    assert all(w["telegram_error"] == "send skipped: no V3 token" and not w["telegram_sent"] for w in would)
    assert all(w["text"] and w["view"] for w in ws)                   # still fully detected + stored
    assert r.counts["errors_detector"] == r.counts["errors_store"] == 0


def test_token_never_in_logs_or_stored_records(tmp_path, monkeypatch, caplog):
    import logging
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_V3", V3_TOKEN)

    class LeakySender(FakeSender):                     # an error text that (wrongly) contains the token
        async def send(self, text):
            raise RuntimeError(f"POST https://api.telegram.org/bot{V3_TOKEN}/sendMessage failed")
    r, store, sim, _ = _setup(tmp_path, sender=LeakySender())
    handler = logging.StreamHandler()
    logging.getLogger().addHandler(handler)
    tg.install_log_redaction()
    try:
        with caplog.at_level(logging.DEBUG):
            caplog.handler.addFilter(tg.RedactFilter())
            logging.getLogger("mias.early_watch_v3").warning("leak test %s", f"bot{V3_TOKEN}")
            _run_all(r, sim)
            _drain(r)
            store.delivery_test("-5328364952", tg.TEST_TEXT, False, None, tg.redact(f"x {V3_TOKEN}"))
    finally:
        logging.getLogger().removeHandler(handler)
    assert V3_TOKEN not in caplog.text and "<V3 token>" in caplog.text
    store.close()
    for p in Path(tmp_path / "v3").glob("v3.db*"):
        assert V3_TOKEN.encode() not in p.read_bytes(), p
    failed = [w for w in V3DB(tmp_path / "v3" / "v3.db").watches() if w["v3_verdict"] == "send"]
    assert failed and all("<V3 token>" in w["telegram_error"] for w in failed)


def test_bot_checks_report_username_chat_and_ids_from_updates(monkeypatch):
    import httpx
    import json
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_V3", V3_TOKEN)
    calls = []

    def handler(req):
        m = req.url.path.rsplit("/", 1)[-1]
        calls.append((m, json.loads(req.content or b"{}")))
        if m == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": "mias_v3_bot"}})
        if m == "getChat":
            return httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"})
        return httpx.Response(200, json={"ok": True, "result": [
            {"my_chat_member": {"chat": {"id": -1005328364952, "type": "supergroup", "title": "MIAS V3"}}},
            {"message": {"chat": {"id": 42, "type": "private"}}}]})
    out = tg.check_chat("-5328364952", transport=httpx.MockTransport(handler))
    assert out["bot"] == "mias_v3_bot" and not out["chat_ok"] and out["chat_error"].endswith("chat not found")
    assert out["chats_in_updates"] == [{"id": -1005328364952, "type": "supergroup", "title": "MIAS V3"}]
    assert [c[0] for c in calls] == ["getMe", "getChat", "getUpdates"]
    assert V3_TOKEN not in json.dumps(out)


def test_delivery_test_record_is_separate_from_watches(tmp_path):
    store = V3DB(tmp_path / "v3.db")
    store.delivery_test("-5328364952", tg.TEST_TEXT, True, "17", None)
    assert store.db.execute("SELECT COUNT(*) FROM v3_delivery_tests").fetchone()[0] == 1
    assert store.watches() == [] and "Watches: **0**" in build(store, ET, str(TODAY), None)
    assert tg.TEST_TEXT == "V3 EARLY WATCH · delivery test · not a Watch"


def test_telegram_send_uses_bold_heading_and_v3_chat(monkeypatch):
    import httpx
    seen = []

    def handler(req):
        import json
        seen.append((str(req.url), json.loads(req.content)))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", V1V2_TOKEN)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_V3", V3_TOKEN)
    snd, _ = tg.make_sender(C.load(V3TOML))
    snd._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    text = Pr.render(view(W("bull", "vwap"), fams=["vwap"]))
    ids = asyncio.run(snd.send(text))
    asyncio.run(snd.close())
    url, body = seen[0]
    assert ids == ["7"] and body["chat_id"] == "-5328364952" and body["text"] == text
    assert f"/bot{V3_TOKEN}/" in url and V1V2_TOKEN not in url
    assert body["entities"][0]["type"] == "bold"


# ---- 6. kill switch ---------------------------------------------------------------------------------------

def test_kill_switch_enabled_false_stops_v3_entirely(tmp_path):
    r, store, sim, p = _setup(tmp_path, sender=FakeSender())
    p.write_text(p.read_text().replace("enabled = true                      #", "enabled = false                     #"))
    sim["t"] = datetime.combine(TODAY, time(12, 0), ET).timestamp()
    asyncio.run(r.step())
    assert r.hot["enabled"] is False and r.last_rowid == 0 and r.counts["bars"] == 0 and not store.watches()
    p.write_text(p.read_text().replace("enabled = false                     #", "enabled = true                      #"))
    sim["t"] += 10
    asyncio.run(r.step())
    assert r.hot["enabled"] is True and r.counts["bars"] > 0               # resumes without any restart


def test_kill_switch_telegram_off_keeps_recording(tmp_path):
    snd = FakeSender()
    r, store, sim, p = _setup(tmp_path, sender=snd,
                              toml_edits=[("telegram_enabled = true ", "telegram_enabled = false")])
    r.reload()
    assert r.hot["telegram_enabled"] is False and r.hot["enabled"] is True
    _run_all(r, sim)
    _drain(r)
    ws = store.watches()
    assert ws and not snd.texts and r.outbox.empty()
    assert all(w["text"] for w in ws)
    assert any(w["telegram_error"] == "not sent: telegram_enabled = false" for w in ws)


def test_family_send_flag_mutes_only_that_family(tmp_path):
    r, store, sim, p = _setup(tmp_path, sender=FakeSender(), toml_edits=[("send_vwap = true", "send_vwap = false")])
    r.reload()
    _run_all(r, sim)
    ws = store.watches()
    assert all(w["v3_verdict"] in ("muted", "skip") for w in ws if w["family"] == "vwap" and w["lane"] == "main")
    assert any(w["v3_verdict"] == "send" for w in ws if w["family"] != "vwap")


def test_policy_pacing_matches_v2():
    p3, p2 = V3Telegram(), obs_v2.V2Telegram()
    base = {"day": "d", "lane": "main", "sym": "A", "side": "bull", "watch_at": 1_000_000}
    seq = [dict(base, family="vwap", leg_id="VW1", watch_id="1"), dict(base, family="or", leg_id="OR1", watch_id="2",
                                                                       watch_at=1_100_000),
           dict(base, family="vwap", leg_id="VW1", watch_id="3", watch_at=1_200_000),
           dict(base, family="vwap", leg_id="VW2", watch_id="4", watch_at=2_000_000),
           dict(base, family="or", leg_id="OR2", watch_id="5", watch_at=9_000_000, lane="late")]
    for w in seq:
        assert p3.decide(w, w["watch_at"]) == p2.decide(w, w["watch_at"])
    assert p3.decide(dict(base, family="vwap", leg_id="VW9", watch_id="9"), 1_000_000 + 181_000) == \
        ("skip", "found late")


# ---- 6b. confluence delivery policy (min_families = 2, 2026-11-24) ----------------------------------------

NAMES = {"structure": "structure", "vwap": "vwap", "or": "opening_range"}


def _session(tmp_path, seed, min_families=2, sender=None):
    edits = [] if min_families == 2 else [("min_families = 2 ", f"min_families = {min_families} ")]
    r, store, sim, p = _setup(tmp_path, seed=seed, sender=sender or FakeSender(), toml_edits=edits)
    _run_all(r, sim)
    _drain(r)
    sim["t"] = datetime.combine(TODAY, time(16, 5), ET).timestamp()
    report = r.end_of_day()
    return r, store, report


def test_config_ships_the_confluence_policy():
    v3 = C.load(V3TOML)
    assert v3.tg.min_families == 2 and all(v3.tg.send.values())
    assert C.hot(V3TOML)["min_families"] == 2
    assert C.TelegramCfg().min_families == 1                 # a file without the key keeps the launch behaviour


def test_single_family_watches_are_created_stored_scored_and_never_sent(tmp_path):
    muted_fams = set()
    for seed in (7, 11):
        snd = FakeSender()
        r, store, report = _session(tmp_path / str(seed), seed, sender=snd)
        ws = [w for w in store.watches() if w["lane"] == "main"]
        single = [w for w in ws if len(set(w["families"])) == 1]
        assert single
        for w in single:
            assert w["v3_verdict"] == "muted" and not w["telegram_sent"] and w["telegram_queued_at"] is None
            assert w["telegram_error"] == f"muted: v3_single_family_muted: {NAMES[w['family']]}"
            assert w["text"] and w["view"] and w["text"] not in snd.texts            # stored, rendered, not sent
            assert w["outcomes_at"] is not None and w["state"] != "WATCH"            # lifecycle + outcomes scored
            assert w["n_support"] + w["n_against"] + w["n_neutral"] == 6
            assert w["chk_options"] in ("support", "against", "neutral") and w["options_status"] is not None
            assert w["options_overall"] is not None and w["chk_spy"] is not None and w["inval_kind"] is not None
            muted_fams.add(w["family"])
        sent = [w for w in store.watches() if w["telegram_sent"]]
        assert sent and len(snd.texts) == len(sent)
        assert all(len(set(w["families"])) >= 2 for w in sent)                    # only confluence reaches Telegram
        assert all(t.startswith("🧭 V3 EARLY WATCH · ") and " + " in t.splitlines()[0] for t in snd.texts)
        assert "## Delivery policy" in report and "Structure only" in report and "Structure + VWAP + OR" in report
        assert "standalone muted: Structure" in report
    assert muted_fams == {"structure", "vwap", "or"}


@pytest.mark.parametrize("seed", [7, 11])
def test_detection_and_scoring_identical_under_both_delivery_policies(tmp_path, seed):
    cols = ("watch_id", "family", "families", "lane", "sym", "side", "watch_at", "watch_price", "level", "state",
            "end_at", "end_reason", "out_5", "out_60", "mfe_pct", "mae_pct", "n_support", "n_against",
            "options_overall", "options_stock", "options_market", "chk_spy", "text")
    runs = {}
    for n in (1, 2):
        _, store, _ = _session(tmp_path / f"m{n}", seed, min_families=n)
        runs[n] = sorted(tuple(str(w[c]) for c in cols) for w in store.watches())
        runs[f"c{n}"] = sorted(store.db.execute("SELECT cand_id, candidate_at, side, status FROM v3_candidate_log"))
    assert runs[1] and runs[1] == runs[2] and runs["c1"] == runs["c2"]


def _w(fam, wid, at, sym="NVDA", side="bear"):
    return {"day": "2026-11-24", "lane": "main", "family": fam, "sym": sym, "side": side, "leg_id": f"L{wid}",
            "watch_id": wid, "watch_at": at, "watch_price": 100.0}


@pytest.mark.parametrize("combo", [("structure", "vwap"), ("structure", "or"), ("vwap", "or"),
                                   ("structure", "vwap", "or")])
def test_confluence_sends_exactly_one_message(tmp_path, combo):
    """14:31 first family alone (muted) · 14:35 second joins inside the combine window (one combined message) ·
    14:36 third joins (merged into it, no second message)."""
    r, store, sim, _ = _setup(tmp_path, sender=FakeSender())
    t0 = int(datetime.combine(TODAY, time(14, 31), ET).timestamp() * 1000)
    open_fams = []
    for i, f in enumerate(combo):
        at = t0 + (0, 240_000, 300_000)[i]
        open_fams.append(f)
        sim["t"] = at / 1000 + 1
        w = _w(f, f"w{i}", at)
        text = f"🧭 V3 EARLY WATCH · {' + '.join(Pr.TAG[x] for x in open_fams)} · NVDA · BEARISH"
        r._deliver(w, {"view": {"families": list(open_fams)}, "text": text})
    _drain(r)
    snd = r.sender
    assert len(snd.texts) == 1 and r.counts["tg_queued"] == 1
    assert snd.texts[0].startswith("🧭 V3 EARLY WATCH · " + " + ".join(Pr.TAG[x] for x in combo[:2]))
    assert not snd.texts[0].startswith(("V3-A EARLY WATCH", "V3-B EARLY WATCH", "V3-C EARLY WATCH"))
    assert r.counts["tg_muted"] == 1 and r.counts["tg_merged"] == len(combo) - 2


def test_policy_decisions_for_each_case():
    p = V3Telegram(min_families=2)
    t = 1_000_000
    assert p.decide(_w("structure", "1", t), t, ["structure"]) == ("muted", "v3_single_family_muted: structure")
    assert p.decide(_w("vwap", "2", t, sym="A"), t, ["vwap"]) == ("muted", "v3_single_family_muted: vwap")
    assert p.decide(_w("or", "3", t, sym="B"), t, ["or"]) == ("muted", "v3_single_family_muted: opening_range")
    assert p.decide(_w("or", "3b", t, sym="B"), t, None)[0] == "muted"          # unknown families = its own only
    assert not p.recent and not p.sent_setups                                     # muting leaves no state behind
    assert p.decide(_w("vwap", "4", t + 240_000), t + 240_000, ["structure", "vwap"]) == ("send", "4")
    assert p.decide(_w("or", "5", t + 280_000), t + 280_000, ["structure", "vwap", "or"]) == ("merged", "4")
    p3 = V3Telegram(min_families=3)
    assert p3.decide(_w("vwap", "6", t), t, ["structure", "vwap"]) == \
        ("muted", "v3_below_min_families: structure + vwap")
    p1 = V3Telegram(min_families=1)
    assert p1.decide(_w("structure", "7", t), t, ["structure"]) == ("send", "7")      # launch behaviour


def test_min_families_is_hot_and_its_history_is_recorded(tmp_path):
    r, store, sim, p = _setup(tmp_path, sender=FakeSender())
    assert r.policy.min_families == 2 and store.get("telegram_policy")[-1]["min_families"] == 2
    p.write_text(p.read_text().replace("min_families = 2 ", "min_families = 1 "))
    sim["t"] += 60
    r.reload()
    assert r.policy.min_families == 1 and [h["min_families"] for h in store.get("telegram_policy")] == [2, 1]
    r.reload()
    assert len(store.get("telegram_policy")) == 2                                 # unchanged value: no new entry


def test_telegram_failure_under_confluence_changes_nothing_else(tmp_path):
    _, ok, _ = _session(tmp_path / "ok", 7)
    _, bad, _ = _session(tmp_path / "bad", 7, sender=FakeSender(fail=True))
    key = lambda st: sorted((w["watch_id"], w["watch_at"], w["state"], w["out_60"], w["v3_verdict"])
                            for w in st.watches())
    assert key(ok) == key(bad)
    failed = [w for w in bad.watches() if w["v3_verdict"] == "send"]
    assert failed and all(not w["telegram_sent"] and "SendError" in (w["telegram_error"] or "") for w in failed)


# ---- 7. V1 / V2 unchanged -------------------------------------------------------------------------------

def test_v1_v2_never_import_v3():
    for f in (ROOT / "mias" / "observer").glob("*.py"):
        assert "early_watch_v3" not in f.read_text(), f
    for f in ("mias/app.py", "mias/engine.py", "mias/notify.py"):
        assert "early_watch_v3" not in (ROOT / f).read_text()


def _git(*a):
    return subprocess.run(["git", "-C", str(ROOT), *a], capture_output=True, text=True)


@pytest.mark.skipif(_git("rev-parse", "checkpoint-2026-11-18-before-v3").returncode != 0,
                    reason="checkpoint tag not available")
def test_v1_v2_files_and_config_identical_to_the_pre_v3_checkpoint():
    paths = ["mias/observer", "config.toml", "deploy/systemd/mias-observer.service", "docs/observer",
             "tests/test_observer_delivery.py", "tests/test_observer_optctx.py", "tests/test_observer_replay.py",
             "tests/test_observer_stream.py", "tests/test_observer_structure.py", "tests/test_observer_telegram.py",
             "tests/test_observer_v2.py", "tests/test_observer_v2_mute.py", "tests/test_observer_watch.py",
             "tests/observer_synth.py"]
    d = _git("diff", "--stat", "checkpoint-2026-11-18-before-v3", "--", *paths)
    assert d.returncode == 0 and d.stdout.strip() == ""
