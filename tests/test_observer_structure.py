"""Observer structure and state: pivots known only at confirmation, developing HL/LH, indicator equality with
mias.indicators, and explicit no-look-ahead (prefix-invariance) tests."""
from datetime import date

from mias.indicators import Bar, atr, ema, session_vwap
from mias.observer.rules import FIVE, ONE
from mias.observer.state import SymbolState, ema_step, five_minute
from mias.observer.structure import Structure
from observer_synth import ET, history, trading_days

T0 = 1_794_000_000_000 // FIVE * FIVE


def b5(i, h, l):
    return Bar(T0 + i * FIVE, (h + l) / 2, h, l, (h + l) / 2, 1000)


def test_ema_step_equals_indicators_ema():
    xs = [100 + (i % 7) - (i % 3) * 0.5 for i in range(200)]
    for period in (9, 21):
        v = None
        for x in xs:
            v = ema_step(v, x, period)
        assert abs(v - ema(xs, period)[-1]) < 1e-9


def test_pivot_is_known_only_at_the_second_following_bar():
    s = Structure()
    highs = [10, 11, 15, 12, 11, 10]
    seen = []
    for i, h in enumerate(highs):
        seen.append(s.on_five(b5(i, h, h - 2)))
    assert seen[2] == [] and seen[3] == []                    # the high at bar 2 is not known yet
    piv = [p for p in seen[4] if p.kind == "H"]
    assert len(piv) == 1 and piv[0].price == 15
    assert piv[0].bar_t == T0 + 2 * FIVE and piv[0].confirmed_at == T0 + 5 * FIVE   # close of bar 4


def test_pivot_labels_hh_hl_lh_ll():
    s = Structure()
    seq = [(10, 8), (12, 9), (14, 11), (12, 9.5), (11, 9), (13, 9.8), (16, 12), (14, 11), (13, 10.5),
           (12, 10), (13, 10.6), (15, 11), (14, 10.9), (13, 10.7)]
    labels = []
    for i, (h, l) in enumerate(seq):
        labels += [(p.kind, p.label) for p in s.on_five(b5(i, h, l))]
    assert ("H", "HH") in labels and ("L", "HL") in labels


def _one(t, o, h, l, c):
    return Bar(t, o, h, l, c, 100)


def test_developing_higher_low_forms_then_fails():
    s = Structure()
    s.pivots = []
    from mias.observer.structure import Pivot
    s.pivots += [Pivot("L", 95.0, T0, T0 + 3 * FIVE, None), Pivot("H", 105.0, T0 + 3 * FIVE, T0 + 6 * FIVE, None)]
    t = T0 + 4 * FIVE                                         # after the swing high's bar
    ev = s.on_one(_one(t, 104, 104, 100, 100.4), atr=2.0)    # low 100 > 95; close 100.4 < 100 + 0.5
    assert ev == [] and s.cand["bull"] is None
    ev = s.on_one(_one(t + ONE, 100.5, 101.2, 100.4, 101.0), atr=2.0)   # close >= 100 + 0.25 x 2
    assert [k for k, _ in ev] == ["DEV_HL"] and s.cand["bull"].level == 100
    ev = s.on_one(_one(t + 2 * ONE, 101, 101, 99.5, 99.8), atr=2.0)     # close below the frozen level
    assert [k for k, _ in ev] == ["DEV_HL_FAIL"] and s.cand["bull"] is None


def test_no_hl_when_the_pullback_breaks_the_prior_swing_low():
    from mias.observer.structure import Pivot
    s = Structure()
    s.pivots += [Pivot("L", 95.0, T0, T0 + 3 * FIVE, None), Pivot("H", 105.0, T0 + 3 * FIVE, T0 + 6 * FIVE, None)]
    t = T0 + 4 * FIVE
    s.on_one(_one(t, 100, 100, 94, 94.5), atr=2.0)           # below 95: a lower low, not a higher low
    ev = s.on_one(_one(t + ONE, 94.5, 97, 94.4, 96.9), atr=2.0)
    assert ev == [] and s.cand["bull"] is None


def _state_run(bars, prior):
    st = SymbolState("SPY", ET)
    st.warm(prior, None)
    st.start_day(date(2026, 11, 11), None)
    return st, [st.on_minute(b) for b in bars]


def test_state_matches_indicator_functions():
    days = trading_days(date(2026, 11, 11), 12)
    h = history(["SPY"], days)["SPY"]
    prior = [(d, h[d]) for d in days[:-1]]
    today = h[days[-1]]
    st, feats = _state_run(today, prior)
    assert abs(feats[-1].vwap - session_vwap(today)) < 1e-6
    all5 = [b for d, bs in prior[-10:] for b in five_minute(bs)] + five_minute(today)
    closes = [b.c for b in all5]
    assert abs(st.ema9_c - ema(closes, 9)[-1]) < 1e-9 and abs(st.ema21_c - ema(closes, 21)[-1]) < 1e-9
    assert abs(st.atr5 - atr(all5[-300:], 14)) < 1e-9


def test_no_look_ahead_prefix_invariance():
    """Every snapshot at bar n is identical whether or not later bars exist: nothing reads the future."""
    days = trading_days(date(2026, 11, 11), 12)
    h = history(["SPY"], days, seed=3)["SPY"]
    prior = [(d, h[d]) for d in days[:-1]]
    today = h[days[-1]]
    _, full = _state_run(today, prior)
    for n in (5, 17, 60, 61, 133, 250, 389):
        _, part = _state_run(today[:n + 1], prior)
        assert part[-1] == full[n]


def test_confirmed_pivots_never_precede_their_confirmation():
    days = trading_days(date(2026, 11, 11), 12)
    h = history(["QQQ"], days, seed=5)["QQQ"]
    st, feats = _state_run(h[days[-1]], [(d, h[d]) for d in days[:-1]])
    for f in feats:
        for p in f.pivots:
            assert p.confirmed_at == f.e                      # surfaced exactly when knowable
            assert p.confirmed_at >= p.bar_t + 3 * FIVE        # k = 2 bars after the pivot bar's close
        for kind, c in f.dev_events:
            assert c.anchor.confirmed_at <= f.e and c.formed_at <= f.e
            if not kind.endswith("FAIL"):
                assert c.formed_at == f.e
