"""2026-11-15 options context for Early Watch: CONTEXT ONLY. It never creates, delays, suppresses or changes a
watch; raw PCR is kept apart from the interpretation; flow leads; stale / thin data is marked; the stored context is
exactly what the message shows."""
import time

from mias.indicators import Bar
from mias.observer import optctx as OC
from mias.observer import optreport
from mias.observer.log import ObserverDB
from mias.observer.message import watch_text
from mias.observer.optctx import OptionsContext, options_lines
from mias.observer.structure import Pivot
from mias.observer.v2 import V2Observer, v2_text
from mias.store import Store
from observer_synth import ET
from test_analytics import NOW_NS, TODAY, _analyze
from fakes import chain, contract
from test_observer_watch import BEAR, DAY, H2, Feed, Observer, feat, hl, kinds, ms

T0 = ms(10, 0) / 1000            # cycle times are seconds


def row(ts, *, cv=1000, pv=1000, flow=(0.5, 0.5), skew=0.0, thin=False, cover=1.0, top=0.05, zero=None,
        label="bullish", cum=(100_000, 120_000), oi=(50_000, 80_000)):
    w = {"ts": ts, "call_volume": cv, "put_volume": pv}
    if zero is not None:
        w.update(call_volume_0dte=zero[0], put_volume_0dte=zero[1])
    o = {"pcr_volume": round(cum[1] / cum[0], 3), "pcr_oi": round(oi[1] / oi[0], 3), "call_volume": cum[0],
         "put_volume": cum[1], "call_oi": oi[0], "put_oi": oi[1], "quote_coverage": cover, "thin": thin,
         "top_volume_share": top, "window": w}
    if zero is not None:
        o.update(call_volume_0dte=cum[0] / 2, put_volume_0dte=cum[1] / 2, call_oi_0dte=oi[0] / 4, put_oi_0dte=oi[1] / 4)
    comps = {"recent_flow": flow[0], "session_flow": flow[1], "put_call": 0.1}
    if skew is not None:
        comps["skew"] = skew
    return {"options_label": label, "options_score": 30.0, "options_components": comps, "options": o,
            "stock_label": "up"}


def metrics_db(tmp_path, series: dict[str, list[tuple[float, dict]]]):
    st = Store(tmp_path / "mias.db")
    by_ts = {}
    for sym, rows in series.items():
        for ts, d in rows:
            by_ts.setdefault(ts, {})[sym] = d
    for ts in sorted(by_ts):
        st.add_metrics(ts, str(DAY), by_ts[ts])
    st.db.close()
    return tmp_path / "mias.db"


def cycles(n, start=T0 - 3600, **kw):
    """n cycles every 150 s ending at `start + 150 (n-1)`; kw values may be callables of the index."""
    out = []
    for i in range(n):
        args = {k: (v(i) if callable(v) else v) for k, v in kw.items()}
        out.append((start + 150 * i, row(start + 150 * i, **args)))
    return out


# ---- rolling PCR maths -------------------------------------------------------------------------------------

def test_rolling_15_and_30_minute_put_call_from_cycle_windows(tmp_path):
    # 24 cycles (60 min). Last 6 windows: 1000 calls / 1500 puts each; the 6 before: 1000 / 1000.
    rows = cycles(24, cv=1000, pv=lambda i: 1500 if i >= 18 else 1000, zero=(400, 900))
    oc = OptionsContext(metrics_db(tmp_path, {"META": rows}), ET)
    end = rows[-1][0]
    ss = oc.samples("META", end)
    assert OptionsContext.rolling(ss, end, 15)["pcr"] == 1.5
    assert OptionsContext.rolling(ss, end - 900, 15)["pcr"] == 1.0
    assert OptionsContext.rolling(ss, end, 30)["pcr"] == 1.25            # (6*1500 + 6*1000) / 12000
    assert OptionsContext.rolling(ss, end, 15, ex0=True)["pcr"] == round(600 / 600, 3)
    pair = oc.roll_pair(ss, end, 15)
    assert pair["cur"] == 1.5 and pair["prev"] == 1.0 and pair["slope"] == 0.5 and pair["dir"] == "rising"


def test_rolling_is_empty_without_coverage_or_volume(tmp_path):
    few = cycles(3, cv=1000, pv=1000)                                     # 7.5 min of windows < 80 % of 15
    oc = OptionsContext(metrics_db(tmp_path, {"META": few}), ET)
    assert OptionsContext.rolling(oc.samples("META", few[-1][0]), few[-1][0], 15) is None
    thin = [(t, row(t, cv=50, pv=50)) for t, _ in cycles(8)]
    oc2 = OptionsContext(metrics_db(tmp_path / "b", {"META": thin}), ET)
    assert OptionsContext.rolling(oc2.samples("META", thin[-1][0]), thin[-1][0], 15) is None


# ---- freshness and data quality -----------------------------------------------------------------------------

def test_stale_snapshot_is_marked_stale_and_shown_as_such(tmp_path):
    rows = cycles(12)
    oc = OptionsContext(metrics_db(tmp_path, {"SPY": rows}), ET)
    t = int((rows[-1][0] + OC.FRESH_S + 5) * 1000)
    c = oc("SPY", t)
    assert c["optx"]["status"] == "stale" and c["optx"]["fresh"] is False and c["optx"]["view"] == OC.INSUFF
    assert c["optx"]["relation"] == {"bull": OC.INSUFF, "bear": OC.INSUFF}
    assert options_lines(c, "bull") == ["○ Options context: STALE / INSUFFICIENT DATA"]
    fresh = oc("SPY", int((rows[-1][0] + 60) * 1000))["optx"]
    assert fresh["status"] == "ok" and fresh["age_s"] == 60 and fresh["snapshot_ts"] == round(rows[-1][0], 1)
    assert fresh["pcr_sample_ts"] and fresh["flow_sample_ts"]


def test_thin_concentrated_or_badly_quoted_options_are_insufficient(tmp_path):
    for kw, why in ((dict(thin=True), "thin"), (dict(top=0.6), "one contract"), (dict(cover=0.3), "quote coverage")):
        rows = cycles(12, **kw)
        oc = OptionsContext(metrics_db(tmp_path / why.replace(" ", "_"), {"PG": rows}), ET)
        x = oc("PG", int(rows[-1][0] * 1000) + 1000)["optx"]
        assert x["status"] == "insufficient" and x["view"] == OC.INSUFF and any(why in r for r in x["reasons"])
        assert options_lines({"optx": x}, "bear") == ["○ Options context: INSUFFICIENT DATA"]


def test_no_context_at_all_still_gives_a_message_line():
    assert options_lines(None, "bull") == ["○ Options context: unavailable"]
    assert options_lines({"options_bias": "bullish"}, "bear") == ["○ Options context: unavailable"]


# ---- interpretation ------------------------------------------------------------------------------------------

def test_flow_leads_pcr_and_skew_only_support_or_contradict():
    assert OC.view_of("bullish", "falling", "bullish") == OC.BULL_C
    assert OC.view_of("bullish", "rising", None) == OC.BULL_C               # one contrary signal does not override
    assert OC.view_of("bullish", "rising", "bearish") == OC.CONFLICT        # both contrary: mixed
    assert OC.view_of("neutral", "falling", "bullish") == OC.NEUTRAL        # PCR cannot create a direction
    assert OC.view_of(None, "falling", "bullish") == OC.INSUFF
    assert OC.relation(OC.BULL_C, "bull") == "CONFIRMING" and OC.relation(OC.BULL_C, "bear") == "CONFLICTING"
    assert OC.relation(OC.CONFLICT, "bull") == "NEUTRAL" and OC.relation(OC.INSUFF, "bear") == OC.INSUFF


def test_examples_from_the_request(tmp_path):
    # bearish watch + bullish recent flow + falling put pressure -> CONFLICTING
    rows = cycles(24, pv=lambda i: 600 if i >= 18 else 1000, flow=(0.6, 0.3))
    oc = OptionsContext(metrics_db(tmp_path / "a", {"SPY": rows}), ET)
    x = oc("SPY", int(rows[-1][0] * 1000) + 1000)["optx"]
    assert x["pcr_dir"] == "falling" and x["flow_dir"] == "bullish"
    assert x["class"]["bear"] == "CONFLICTING" and x["class"]["bull"] == "CONFIRMING"
    assert options_lines({"optx": x}, "bear")[0] == "⚠️ Options context: CONFLICT"
    assert options_lines({"optx": x}, "bull") == ["✓ Options context: CONFIRMING", "   Flow: bullish | PCR: falling | "
                                                  "Skew: neutral"]
    # bearish watch + neutral flow + no usable PCR -> NEUTRAL
    rows = cycles(12, cv=10, pv=10, flow=(0.05, -0.05))
    x = OptionsContext(metrics_db(tmp_path / "b", {"SPY": rows}), ET)("SPY", int(rows[-1][0] * 1000))["optx"]
    assert x["pcr_dir"] is None and x["class"]["bear"] == "NEUTRAL"


def test_market_context_for_single_stocks_and_mixed_overall(tmp_path):
    bear = dict(flow=(-0.6, -0.4))
    rows = {"META": cycles(12, **bear), "SPY": cycles(12, flow=(0.6, 0.4)), "QQQ": cycles(12, flow=(0.5, 0.1))}
    x = OptionsContext(metrics_db(tmp_path, rows), ET)("META", int(rows["META"][-1][0] * 1000))["optx"]
    assert x["view"] == OC.BEAR_C and x["market_view"] == OC.BULL_C
    assert x["relation"]["bear"] == "CONFIRMING" and x["overall"]["bear"] == "MIXED"
    assert options_lines({"optx": x}, "bear")[-1] == ("   Stock options: BEARISH | Market options: BULLISH | "
                                                      "Overall: MIXED")
    assert OC.market_view([OC.BULL_C, OC.BEAR_C]) == OC.CONFLICT and OC.market_view([OC.INSUFF, OC.NEUTRAL]) == OC.NEUTRAL
    spy = OptionsContext(metrics_db(tmp_path / "s", rows), ET)("SPY", int(rows["SPY"][-1][0] * 1000))["optx"]
    assert spy["market"] is None and spy["overall"] == spy["relation"]       # index: no market overlay


# ---- divergence (research) -----------------------------------------------------------------------------------

def _div_case(tmp_path, kind):
    # 48 cycles (2 h). PCR 1.5 in the first hour, 0.9 in the second.
    rows = cycles(48, start=T0 - 7200, pv=lambda i: 1500 if i < 24 else 900)
    if kind == "H":
        pv = [Pivot("H", 100.0, int((T0 - 5400) * 1000), int((T0 - 4800) * 1000), "HH"),
              Pivot("H", 101.0, int((T0 - 1500) * 1000), int((T0 - 900) * 1000), "HH")]
    else:
        pv = [Pivot("L", 100.0, int((T0 - 5400) * 1000), int((T0 - 4800) * 1000), "LL"),
              Pivot("L", 99.0, int((T0 - 1500) * 1000), int((T0 - 900) * 1000), "LL")]
    oc = OptionsContext(metrics_db(tmp_path, {"SPY": rows}), ET, pivots=lambda s: pv)
    return oc("SPY", int(rows[-1][0] * 1000) + 1000)["optx"]


def test_bearish_divergence_higher_high_with_falling_pcr(tmp_path):
    x = _div_case(tmp_path, "H")
    assert x["divergence"] == OC.BEAR_DIV and "higher high" in x["div_detail"]
    assert x["class"]["bull"] == "BEARISH_DIVERGENCE" and x["class"]["bear"] == "BEARISH_DIVERGENCE"
    assert x["relation"]["bull"] in ("CONFIRMING", "NEUTRAL", "CONFLICTING")      # relation kept separately
    shown = options_lines({"optx": x}, "bull")
    assert shown[0] == {"CONFIRMING": "✓ Options context: CONFIRMING", "NEUTRAL": "○ Options context: NEUTRAL",
                        "CONFLICTING": "⚠️ Options context: CONFLICT"}[x["relation"]["bull"]]
    assert shown[-1] == "   PCR divergence: potential bearish (weak context, not a reversal signal)"


def test_bullish_divergence_lower_low_with_lower_pcr(tmp_path):
    x = _div_case(tmp_path, "L")
    assert x["divergence"] == OC.BULL_DIV and x["class"]["bear"] == "BULLISH_DIVERGENCE"


def test_no_divergence_without_a_new_extreme_or_pcr(tmp_path):
    rows = cycles(48, start=T0 - 7200)                                    # flat PCR
    pv = [Pivot("H", 100.0, int((T0 - 5400) * 1000), int((T0 - 4800) * 1000), "HH"),
          Pivot("H", 101.0, int((T0 - 1500) * 1000), int((T0 - 900) * 1000), "HH")]
    x = OptionsContext(metrics_db(tmp_path, {"SPY": rows}), ET, pivots=lambda s: pv)("SPY", int(rows[-1][0] * 1000))
    assert x["optx"]["divergence"] is None


# ---- never changes a watch ----------------------------------------------------------------------------------

class Boom:
    def __call__(self, sym, ts):
        raise RuntimeError("options down")


def _v1(context):
    ob = Observer(["META"], DAY, ET, context=context)
    f = Feed(ob)
    return f.run([feat(10, 10), feat(10, 11), feat(10, 30, **BEAR), feat(10, 31, **BEAR)])


def _strip(evs):
    return [{k: v for k, v in e.items() if k != "context"} for e in evs]


def test_v1_watches_identical_with_any_context(tmp_path):
    rows = {"META": cycles(40, start=T0 - 3600, flow=(-0.8, -0.8)), "SPY": cycles(40, start=T0 - 3600),
            "QQQ": cycles(40, start=T0 - 3600)}
    oc = OptionsContext(metrics_db(tmp_path, rows), ET)
    plain, enriched, broken = _v1(None), _v1(oc), _v1(Boom())
    assert len(kinds(plain, "watch")) == 1
    assert _strip(plain) == _strip(enriched) == _strip(broken)
    w = kinds(enriched, "watch")[0]
    assert w["created_at"] == ms(10, 12)                                   # not delayed
    assert w["context"]["optx"]["class"]["bull"] == "CONFLICTING"         # conflicting context: still a watch


def _v2(context):
    ob = V2Observer(["META"], DAY, ET, context=context)
    q = []
    ob.states["META"].on_minute = lambda bar, gap=False: q.pop(0)
    c, c2 = hl(), hl(anchor=H2, level=100.5)
    out = []
    for f in [feat(10, 10, hl=None), feat(10, 11, hl=c, dev_events=(("DEV_HL", c),)), feat(10, 12, hl=c),
              feat(10, 20, hl=c2, dev_events=(("DEV_HL", c2),)), feat(10, 21, hl=c2), feat(10, 22, hl=c2)]:
        q.append(f)
        out += ob.process("META", Bar(f.t, f.o, f.h, f.l, f.c, f.v))
    return out + ob.finalize()


def test_v2_watches_identical_with_any_context(tmp_path):
    rows = {"META": cycles(40, start=T0 - 3600, flow=(-0.8, -0.8)), "SPY": cycles(40, start=T0 - 3600),
            "QQQ": cycles(40, start=T0 - 3600)}
    oc = OptionsContext(metrics_db(tmp_path, rows), ET)
    plain, enriched, broken = _v2(None), _v2(oc), _v2(Boom())
    assert [e for e in plain if e["kind"] == "v2_watch"]
    assert _strip(plain) == _strip(enriched) == _strip(broken)
    ws = [e for e in enriched if e["kind"] == "v2_watch"]
    assert all("optx" in w["context"] for w in ws)
    assert all("context_error" in w["context"] for w in broken if w["kind"] == "v2_watch")


def test_messages_still_build_when_options_are_unavailable(tmp_path):
    w = kinds(_v1(Boom()), "watch")[0]
    assert "○ Options context: unavailable" in watch_text(w, ET)
    v2w = [e for e in _v2(None) if e["kind"] == "v2_watch"][0]
    txt = v2_text(v2w, [v2w["family"]], ET)
    assert "○ Options context: unavailable" in txt and txt.endswith("RESEARCH WATCH ONLY — NOT A TRADE SIGNAL")


def test_stored_context_reproduces_the_displayed_message(tmp_path):
    rows = {"META": cycles(40, start=T0 - 3600, flow=(0.7, 0.4)), "SPY": cycles(40, start=T0 - 3600),
            "QQQ": cycles(40, start=T0 - 3600)}
    oc = OptionsContext(metrics_db(tmp_path, rows), ET)
    evs = _v1(oc)
    shown = watch_text(kinds(evs, "watch")[0], ET)
    db = ObserverDB(tmp_path / "obs.db")
    db.apply(evs, "live")
    stored = db.watches()[0]
    assert watch_text(stored, ET) == shown and "✓ Options context: CONFIRMING" in shown
    assert stored["context"]["optx"]["source"] == "live snapshot"


def test_lookup_is_fast_once_cached(tmp_path):
    rows = {"META": cycles(150, start=T0 - 6 * 3600), "SPY": cycles(150, start=T0 - 6 * 3600),
            "QQQ": cycles(150, start=T0 - 6 * 3600)}
    oc = OptionsContext(metrics_db(tmp_path, rows), ET)
    oc.warm(["META", "SPY", "QQQ"], T0)
    t = time.perf_counter()
    for i in range(20):
        oc("META", int(T0 * 1000) - i * 60_000)
    assert (time.perf_counter() - t) / 20 < 0.05


# ---- report grouping -------------------------------------------------------------------------------------------

def test_report_groups_by_class_and_keeps_sources_apart():
    def w(cls, side="bull", src="live snapshot", r=0.1, sym="META"):
        x = {"source": src, "class": {side: cls}, "relation": {side: cls}, "overall": {side: cls}, "status": "ok"}
        return {"side": side, "sym": sym, "t": ms(10, 0), "day": str(DAY), "family": "structure", "price": 100.0,
                "context": {"optx": x}, **{f"r{n}": r for n in (5, 10, 15, 30, 60)}}
    rows = [w("CONFIRMING"), w("CONFIRMING", r=-0.1), w("CONFLICTING", r=-0.2),
            w("CONFIRMING", src="post-hoc reconstructed"), {"side": "bear", "sym": "SPY", "t": ms(10, 0),
                                                                    "day": str(DAY), "price": 1.0, "context": {}}]
    g = dict((k, len(v)) for k, v in optreport.by(rows, optreport.klass))
    assert g == {"CONFIRMING": 2, "CONFLICTING": 1, "CONFIRMING [post-hoc reconstructed]": 1, "not recorded": 1}
    tab = optreport.group_table(optreport.by(rows, optreport.klass))
    assert any(l.startswith("| CONFIRMING | 2 | 50% (2)") for l in tab)
    text = "\n".join(optreport.lines([], {"structure": rows}, {"structure": "V2-A Structure"}, ET))
    assert "12c. Do V2-A Structure" in text and "Structure · CONFLICTING" in text


# ---- production options snapshot: additive fields only --------------------------------------------------------

def test_snapshot_0dte_split_and_concentration_do_not_change_the_bias_inputs():
    items = chain("X", 100, TODAY, NOW_NS) + [contract("X", "put", 100, TODAY, spot=100, volume=500, now_ns=NOW_NS),
                                              contract("X", "call", 100, TODAY, spot=100, volume=100, now_ns=NOW_NS)]
    snap, vols = _analyze(items, None)
    assert snap.put_volume_0dte == 500 and snap.call_volume_0dte == 100
    assert snap.call_volume == 100 + 10 * 15 and snap.put_volume == 500 + 10 * 15
    assert snap.top_volume_share == round(500 / (snap.call_volume + snap.put_volume), 4)
    more = {c["details"]["ticker"]: (vols[c["details"]["ticker"]] + 50) for c in items}
    nxt = [dict(c, day=dict(c["day"], volume=more[c["details"]["ticker"]])) for c in items]
    snap2, _ = _analyze(nxt, vols)
    assert snap2.window.put_volume_0dte == 50 and snap2.window.call_volume_0dte == 50
    assert snap2.window.put_volume == 50 * 16 and snap2.window.call_volume == 50 * 16
