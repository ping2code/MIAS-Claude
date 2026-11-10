"""[alerts] tier1_trend = "swing", [alerts] symbols and the morning roll-call (2026-11-09)."""
from __future__ import annotations

import dataclasses
from datetime import datetime, time

import httpx

from mias.signals import evaluate_tiered, rollcall_reason
from mias.app import Service
from test_service import ET, TODAY, FakeMassive, _aligned, _engine, _result, cfg  # noqa: F401 (fixture)


def test_swing_mode_alerts_when_the_swing_trend_turns_not_on_strength(cfg):
    cfg = dataclasses.replace(cfg, tier1_trend="swing")
    state, t, seen = {}, 1_000_000.0, []
    steps = [(10, 0, 0), (40, 0, 0), (42, 0, 0),           # NEUTRAL -> UP: tier 1
             (70, 70, 55), (72, 72, 55),                    # UP -> STRONG UP (+ full alignment): same side, no tier 1
             (-40, 0, 0), (-42, 0, 0)]                      # STRONG UP -> DOWN: tier 1, bearish
    for i, (sw, intra, opt) in enumerate(steps):
        t1, t2 = evaluate_tiered(_aligned(sw, intra, opt), state, cfg, t + 150 * i, [])
        seen += [(i, x.rule, x.direction, x.text) for x in t1]
        assert "strong_alignment" not in [x.rule for x in t1]
        if i in (2, 4, 6):
            assert "trend_change" in [x.rule for x in t2]   # the Changes digest still gets every change
    assert [(i, r, d) for i, r, d, _ in seen] == [(2, "swing_trend", "bull"), (6, "swing_trend", "bear")]
    assert seen[0][3] == "SWING TREND UP: NEUTRAL → UP (+42)"
    assert seen[1][3] == "SWING TREND DOWN: STRONG UP → DOWN (-42)"


def test_strong_mode_unchanged(cfg):
    cfg = dataclasses.replace(cfg, tier1_trend="strong")
    state, t, fired = {}, 1_000_000.0, []
    for i, (sw, intra, opt) in enumerate([(10, 0, 0), (40, 0, 0), (42, 0, 0), (70, 70, 50), (72, 72, 52)]):
        t1, _ = evaluate_tiered(_aligned(sw, intra, opt), state, cfg, t + 150 * i, [])
        fired += [x.rule for x in t1]
    assert fired == ["strong_alignment"]                   # NEUTRAL -> UP alone does not alert in strong mode


def test_full_alignment_is_noted_and_rollcall_reason():
    full = rollcall_reason(_aligned(70, 70, 55), None)
    assert full.direction == "bull" and full.text == "MORNING ROLL-CALL · SWING STRONG UP (+70) · ✅ full strong alignment"
    assert rollcall_reason(_aligned(-30, 0, 0), None).text == "MORNING ROLL-CALL · SWING DOWN (-30)"
    assert rollcall_reason(_aligned(10, 0, 0), None) is None
    assert rollcall_reason(_aligned(20, 0, 0), "up").direction == "bull"     # the confirmed class wins


def _healthy(score, symbol="AAA"):
    r = dataclasses.replace(_result(score, 0), symbol=symbol)
    r.options.quote_coverage = 1.0                 # the helper's snapshot has no live quotes: the health gate holds it
    return r


def _turn_up(eng, now):
    for k, score in enumerate((10, 42, 42)):
        eng._evaluate([_healthy(score)], now.replace(minute=k * 3))


def test_alerts_only_for_listed_symbols_but_state_stays_current(cfg):
    cfg = dataclasses.replace(cfg, alert_mode="tiered", tier1_trend="swing", alert_symbols=("BBB",))
    now = datetime.combine(TODAY, time(11, 0), ET)
    eng, store, _ = _engine(cfg, FakeMassive(TODAY, now, {"AAA": 100.0}))
    _turn_up(eng, now)
    assert store.pending() == [] and (store.get("changes") or {}).get("events", []) == []
    assert store.get("state:AAA")["swing"]["cls"] == "up"                    # still tracked
    assert "AAA" in eng.swing_changes                                         # position alerts still see it
    assert not eng.alerts_for("AAA") and eng.alerts_for("BBB")


def test_listed_symbol_gets_the_swing_alert(cfg):
    cfg = dataclasses.replace(cfg, alert_mode="tiered", tier1_trend="swing", alert_symbols=("AAA",))
    now = datetime.combine(TODAY, time(11, 0), ET)
    eng, store, _ = _engine(cfg, FakeMassive(TODAY, now, {"AAA": 100.0}))
    _turn_up(eng, now)
    sent = store.pending()
    assert len(sent) == 1 and sent[0][1] == "swing_trend" and "SWING TREND UP: NEUTRAL → UP (+42)" in sent[0][2]


def test_morning_rollcall_once_for_trending_alert_symbols_in_order(cfg):
    cfg = dataclasses.replace(cfg, alert_mode="tiered", rollcall_time="09:45", alert_symbols=("BBB", "AAA"))
    now = datetime.combine(TODAY, time(9, 47), ET)
    svc = Service(cfg, transport=httpx.MockTransport(FakeMassive(TODAY, now, {"AAA": 100.0}).handler))
    up, down, flat = _healthy(45), _healthy(-70, "BBB"), _healthy(5, "CCC")
    svc.engine.last_results = [up, down, flat]
    svc.engine.last_cycle = {"at": now.isoformat()}
    svc._send_rollcall(now)
    sent = svc.store.pending()
    assert [r for _, r, *_ in sent] == ["rollcall", "rollcall"]
    assert "SWING STRONG DOWN (-70)" in sent[0][2] and "BBB" in sent[0][2]   # [alerts] symbols order
    assert "SWING UP (+45)" in sent[1][2] and "AAA" in sent[1][2]
    svc._send_rollcall(now)
    assert len(svc.store.pending()) == 2                                      # once a day
    early = datetime.combine(TODAY, time(9, 40), ET)
    svc2 = Service(dataclasses.replace(cfg, db_path=cfg.db_path.with_name("r2.db")))
    svc2.engine.last_results, svc2.engine.last_cycle = [up], {"at": early.isoformat()}
    svc2._send_rollcall(early)
    assert svc2.store.pending() == []                                          # not before 09:45
