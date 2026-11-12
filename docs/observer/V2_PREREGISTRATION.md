# Early Watch v2 — research pre-registration (frozen 2026-11-11, before any v2 code or result)

**Status: POST-HOC hypotheses.** Everything here was conceived *after* reviewing the frozen v1 replay (40 days,
2026-09-17 … 2026-11-11), in particular that v1 requires provisional EMA9 ≥ EMA21 and so behaves like an early
version of Strategy B (74 of 85 v1 upgrades were B entries, mostly 0–3 minutes later). v2 is **not** claimed to have
been pre-registered before v1. It is a separate research track; v1 (`early-watch-v1`, `docs/observer/PREREGISTRATION.md`,
its code, database, replay and Telegram pilot) stays unchanged as the control.

Rules version `early-watch-v2-research`. Nothing below changes after results are seen; Nov 12, 2026 live data must
not change it. Anything noticed later is POST-HOC and is not turned into a rule without a new pre-registration.

A v2 watch means only: *a move may be developing earlier than EMA confirmation — research watch.* No trades, no
contract, no change to B / C / D / E7 / E21 / Simple / close fade, Strong Alignment, swing / trend alerts, options
flow or v1.

## 0. v1 terminology (resolved before v2; reporting only, v1 unchanged)

In the v1 replay, "watches" (1,175 main-lane, median 29 / day) were **actual watch events created**, not raw
candidates. "≤ 11 per day" was v1's pre-registered *noise acceptance threshold*, not a cap. The caps (3 per symbol per
day × 11 symbols = 33; 6 per rolling 60 min over 09:45–15:00 ≈ 31) permit ~31–33 per day, so 29 means the caps were
binding most of the day. Audit of the replay database: max 3 main watches per symbol per day; max 6 in any rolling
60 minutes; 0 Telegram sent. Funnel (main lane): 3,355 candidate legs → 1,175 watched legs (1,175 watch events) →
2,180 legs never watched (first binding reason per leg: after 15:00 475, hourly cap 466, open watch 363, cooldown
242, symbol cap 152, combinations for the rest). Reports now show RAW CANDIDATE LEGS → SUPPRESSED (by reason) →
WATCHES → TELEGRAM SENT.

## 1. Shared definitions (all three families)

* Symbols: the observer's 11 (SPY QQQ IWM NVDA TSLA META MSFT AAPL AMZN AMD PG). Data, warm-up, VWAP, opening range,
  ATR, confirmed pivots and developing HL / LH come from the **unchanged** v1 state engine (`mias/observer/state.py`,
  `structure.py`: k = 2, 0.25 × ATR(14, 5m)), run as separate instances fed the same completed 1-minute bars.
* Processing order per bar: confirmed events before the bar end applied → state update → open watches (expiry, then
  invalidation) → pending candidates (watch or not confirmed) → new candidates. Bars in (bar end, symbol order).
* **CANDIDATE** = the family's raw trigger at a completed 1-minute close. **WATCH** = the candidate passed its
  persistence rule. **TELEGRAM_SENT** = an operational decision (section 5), separate from the research record.
* Health: the candidate bar and every persistence bar must not be stale (v1 gap rule).
* One open watch per family / symbol / side; a candidate arising while one is open is logged `open_watch_exists`.
  Duplicate legs per family / side are logged `same_leg`. **No cooldown, chop lock or watch caps** (noise is measured).
* Lanes by the watch bar: `main` = starts ≥ 09:45 and ends ≤ 15:00; `opening` = starts 09:30–09:45 (research only);
  `late` = ends after 15:00 (research only). Candidates are logged all session.
* Lifecycle of a watch: **UPGRADED** by the v1 confirmed-event sources (B, C, D, E7, E21, Simple entries; swing_trend,
  strong_alignment alerts), same symbol and side, `watch_at ≤ ts ≤ watch_at + 60 min`, price = last 1-minute close
  at or before the event; **EXPIRED** at the first bar ending ≥ watch_at + 60 min (or 16:00); **INVALIDATED** by the
  family rule. Research continues after an upgrade only in the outcome fields.
* EMA is **context only**, never a gate. Recorded at candidate and watch: provisional EMA9 / EMA21, spread, spread
  change vs the last completed 5-minute spread, completed-EMA9 slope (change over the last completed 5-minute bar),
  `ema_crossed` on completed 5-minute bars (bull: EMA9 ≥ EMA21; bear: ≤) and on provisional values.
  **pre_ema** (primary) = the completed 5-minute EMA had **not** crossed in the watch's direction at the watch.
  Completed-5m EMA cross events are logged, giving minutes from the watch to the next cross in its direction (same
  day).
* **Swing-leg start** (no look-ahead): bull = the latest confirmed swing-low pivot whose `confirmed_at` ≤ the candidate
  time (its price); bear = the latest confirmed swing high. None if none exists.

## 2. V2-A — STRUCTURE FIRST

* Bull CANDIDATE at bar t: the v1 structure engine forms a developing Higher Low at t (event `DEV_HL`; its formation
  already requires a close ≥ dev_low + 0.25 × ATR). Bear: `DEV_LH`.
* WATCH at t + 1: the next processed close keeps that same candidate alive (same anchor and level; not failed).
  Otherwise the candidate ends `not_confirmed`.
* Leg = `P<anchor pivot bar_t>` (one watch per leg). Level = the candidate level.
* INVALIDATED: a completed close beyond the level (bull: below).

## 3. V2-B — VWAP RECLAIM / REJECTION FIRST

* Bull CANDIDATE at bar t: close_t > VWAP_t **and** each of the 3 previous processed closes was ≤ its VWAP.
  Bear: close_t < VWAP_t and the 3 previous closes ≥ VWAP.
* WATCH at t + 2: the next 2 processed closes are both beyond VWAP on the same side; otherwise `not_confirmed`.
* Leg = `VW<candidate bar start>`. Level (display) = VWAP at the watch.
* INVALIDATED: 2 consecutive completed closes back through VWAP (bull: ≤ VWAP).

## 4. V2-C — OPENING RANGE FIRST

* Opening range = the existing 09:30–09:45 1-minute range. Bull CANDIDATE at bar t (bar starts ≥ 09:45):
  close_t > OR high and the previous processed close ≤ OR high. Bear: close_t < OR low, previous ≥ OR low.
* WATCH at t + 2: the next 2 processed closes stay beyond the edge; otherwise `not_confirmed`.
* Leg = `OR<candidate bar start>` (a later re-break is a new candidate). Level = the OR edge.
* INVALIDATED: a completed close back inside the range past the edge (bull: < OR high).

## 5. Telegram (operational, frozen for the pilot; never part of the research record)

* Eligible: `main`-lane v2 watches created live and found within 180 s (no restart backfill re-sends).
* At most one message per family / symbol / side / setup (setup = leg for A and B; `OR<day>` + side for C).
* Combining: if a v2 message for the same symbol and side was sent in the last 5 minutes, a new family watch is not
  sent; it is recorded as merged into that message. A message lists every family with an open watch on that
  symbol / side at send time (✓) and the others (○). One family → "🧭 V2 STRUCTURE WATCH" / "🧭 V2 VWAP WATCH" /
  "🧭 V2 OR WATCH"; two or more → "🧭 V2 EARLY WATCH".
* At most 6 v2 messages per rolling 60 minutes (independent of v1's caps). None before 09:45 or after 15:00 ET.
* Text: symbol, BULLISH / BEARISH (the stock setup only), time, price, the triggering families, EMA context (crossed
  or not), VWAP side, volume, SPY, the family level, and the last line **RESEARCH WATCH ONLY — NOT A TRADE SIGNAL**.
  No strike, expiry, contract, entry, target, stop or instruction.
* Plain text, same bot and alert chats; separate queue and worker; failures recorded, never blocking.

## 6. Periods (chosen before any v2 performance was computed)

* **Development:** 2026-07-01 … 2026-09-16 (older than the v1 window): implementation verification and exploratory
  research only.
* **Holdout:** 2026-09-17 … 2026-11-11 (the v1 replay window). Caveat: the v2 ideas came from v1's aggregate results on
  this window, so it is not fully clean; v2's own performance on it has not been computed before this commit, and it
  must not be used to change v2.
* **Forward:** 2026-11-12 onward — the first clean out-of-sample observation.

## 7. Metrics and acceptance (per family; applied to holdout and forward separately)

Primary: candidates, watches, Telegram sent, not-confirmed / invalidated / expired / upgraded rates, lead time
(vs the first later B entry; vs any confirmed event), price advantage, signed r5 / r15 / r30 / r60, MFE / MAE (watch →
upgrade or end), pre-EMA %, minutes before the EMA cross, session decomposition (open → watch → confirmation → +60 min),
swing-leg decomposition (leg start → candidate → watch → confirmation → +60 min), symbol / day concentration,
time of day, side, SPY direction. Secondary: later B, later C / D / E7 / E21, later swing / strong alert, no later
signal.

* **PROMISING** only if all hold on the holdout **and** on ≥ 10 forward sessions: pre-EMA ≥ 50% of main watches;
  median minutes before the EMA cross ≥ 10 (pre-EMA watches with a later cross); median r15 > 0 and r60 > 0; median
  MFE / |median MAE| ≥ 1.2; invalidation ≤ 60%; median main watches per day ≤ 22; the sign of median r60 holds
  without the top symbol and without the top day.
* **REJECT** if on the holdout median r60 ≤ 0 **and** median MFE / |median MAE| < 1.0.
* Otherwise **INCONCLUSIVE**.
* A single live day is only classified as: LIVE PILOT CONSISTENT WITH HOLDOUT · BETTER THAN HOLDOUT — NEED MORE DAYS ·
  WORSE THAN HOLDOUT · FEED / OPERATIONAL ISSUE.

No v2 rule, threshold, filter, cap or Telegram setting changes during or because of the Nov 12 session.
