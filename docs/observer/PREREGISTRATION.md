# Early Watch v1 — pre-registration (frozen 2026-11-11, before any replay or live result)

Rules version `early-watch-v1`. Code: `mias/observer/` (constants in `rules.py`). Nothing below may be changed after
results are seen; a change is a new version with a new pre-registration. Anything noticed in the results that is not
listed here is reported as **POST-HOC** and is not turned into a rule.

An Early Watch means only: *something may be developing — this symbol may be worth looking at.* It is not a trade
signal, opens no paper trade, names no contract, and changes nothing in B / C / D / E7 / E21 / Simple / close fade,
Strong Alignment, swing / trend alerts, option flow or Telegram. Shadow mode: no Telegram is sent (no sender exists).

## A. Data and Phase A acceptance

* Stream: Massive stocks WebSocket `AM` (per-minute aggregates), one connection, the observer symbols only
  (`[observer] symbols` = SPY QQQ IWM NVDA TSLA META MSFT AAPL AMZN AMD PG). No second aggregates, trades, quotes or
  options streams.
* Logic uses the bar timestamps (`s`, `e`); the receive time is stored separately for latency.
* Bars are de-duplicated by symbol + start + end; a bar older than the newest seen for that symbol is recorded as
  out-of-order and never evaluated; a bar with the same window but different values is recorded as `revised` and not
  re-evaluated.
* A gap since a symbol's last processed bar is filled from REST 1-minute aggregates before the new bar is evaluated.
  If REST cannot be reached, the bar is evaluated with `gap = true` and the symbol is stale for 2 bars. After every
  reconnect all symbols are backfilled from REST first.
* Evaluation happens per minute once every symbol has reported it or 5 s after the minute ended, in fixed symbol order
  (SPY first).
* **Acceptance (per regular session):** median arrival latency ≤ 5 s and p99 ≤ 30 s after bar end; stream bars missing
  vs REST ≤ 1%; close equal to REST (± $0.01) on ≥ 99% of matched bars; no unrecovered gap. OHLCV, bar VWAP and
  session VWAP (`a`, compared with REST VWAP from 09:30 and from 04:00) differences are reported. Telegram may not be
  proposed before at least 3 live sessions pass.

## B. State (per symbol, completed regular-session 1-minute bars only)

* Warm-up: the prior 10 regular sessions of REST 1-minute bars; 5-minute bars are clock-aligned merges of 1-minute
  bars (live and replay use the same function). The last prior session is replayed minute by minute so pivots and
  developing legs exist at the open. Previous close = last regular-session 1-minute close of the prior session.
* Session VWAP from 09:30 (typical price × volume, as `mias.indicators.session_vwap`).
* EMA 9 / 21 (as `mias.indicators.ema`) advance only on completed 5-minute bars. Provisional EMA = one more EMA step
  with the forming 5-minute close (never stored). Spread = EMA9 − EMA21; reference spread = the spread at the last
  completed 5-minute bar before the current one.
* ATR(14) on the last 300 completed 5-minute bars (as `mias.indicators.atr`). Opening range = the 09:30–09:45
  1-minute bars; complete from the bar ending 09:45.
* Context only, never a gate: relative volume (today's cumulative volume vs the prior sessions' average at the same
  minute), SMA200 side (daily closes), SPY day % at the watch minute (the observer's own SPY state, no later bar),
  RSI(14) and MACD(12, 26, 9) histogram on 5-minute closes, MIAS options bias (latest MIAS cycle at or before the
  watch, read-only).

## C. Structure

* **CONFIRMED pivot:** 5-minute bar *i* with a high strictly above the highs of bars *i−2, i−1, i+1, i+2* (swing high)
  or a low strictly below their lows (swing low). Known only at the close of bar *i+2* (`confirmed_at`); both `bar_t`
  and `confirmed_at` are stored. Labels vs the previous pivot of the same kind: HH / LH (EH if equal), HL / LL (EL).
  Pivots are append-only; nothing is rewritten (no ZigZag).
* **DEVELOPING Higher Low:** after the latest confirmed swing high H, `dev_low` = the lowest 1-minute low since H's bar.
  A candidate exists when `dev_low` > the latest confirmed swing low before H and a completed 1-minute close ≥
  `dev_low` + 0.25 × ATR. Its level is frozen at formation; a completed 1-minute close below the level fails it.
* **DEVELOPING Lower High:** mirror (after the latest swing low L, `dev_high` < the swing high before L, close ≤
  `dev_high` − 0.25 × ATR; fails on a close above the level).
* k = 2 and 0.25 × ATR are frozen starting research values.

## D. Early Watch criteria (main lane: bars starting ≥ 09:45 and ending ≤ 15:00 ET)

A bullish watch is created at the close of a 1-minute bar *t* when **all** of these hold at *t* **and** at the
previous processed close *t−1* (the "2 completed closes"):

1. close > session VWAP;
2. provisional EMA9 ≥ EMA21;
3. provisional spread > reference spread (widening);
4. structure: a developing Higher Low (the same leg at both closes), **or** close > opening-range high at both closes
   (break held 2 closes);

and the symbol is not stale (at *t* or *t−1*), has no open watch, is not in cooldown or chop lock, the leg was not used
yet, fewer than 3 main watches today for the symbol, fewer than 6 main watches in the last 60 minutes across all
symbols. Bearish mirrors it (close < VWAP, EMA9 ≤ EMA21, spread narrowing, developing Lower High or close <
opening-range low on both closes). If both a developing HL/LH and an opening-range break hold, the type is HL/LH.
Bull is evaluated before bear; at most one new watch per symbol per bar per lane.

* **Leg id** (one watch per symbol + side + leg): `P<bar_t of the anchoring swing high / low>` for HL/LH;
  `OR<date>` for opening-range breaks (at most one per side per day).
* **Invalidation level:** the developing swing low / high; for opening-range breaks the broken range edge.
* **Opening lane (research only):** bars starting 09:30–09:45, same criteria (the opening range is not complete, so
  only HL/LH), `opening_shadow_only = true`, no caps / cooldown / chop lock, own leg set, never actionable, never
  affects the main lane.
* **Suppression:** criteria met but no watch is logged once per lane / symbol / side / leg / reason, with the first
  binding reason in this order: after 15:00, stale, a watch already open, chop lock, cooldown, same leg, symbol cap,
  hourly cap.

## E. Lifecycle (per symbol and lane)

Order for each bar: (1) confirmed events with timestamp before the bar's end are applied, (2) the state takes the
bar, (3) expiry then invalidation of the open watch, (4) creation. Bars are processed in (bar end, symbol order).

* **UPGRADED:** an existing confirmed event — B, C, D, E7, E21 or Simple entry, or a swing_trend or strong_alignment
  alert — for the same symbol and side with `created_at ≤ ts ≤ created_at + 60 min`. Upgrade price = the close of the
  last 1-minute bar ending at or before the event. No cooldown after an upgrade (the leg is used).
* **EXPIRED:** the first bar ending ≥ `created_at + 60 min` without an upgrade (also any watch still open at 16:00).
* **INVALIDATED** (bull; bear mirrors): a completed 1-minute close below the level; or 2 consecutive closes below VWAP;
  or a completed 5-minute bar with EMA9 < EMA21; or the bearish criteria (D 1–4 on 2 closes) hold. *Known property:*
  bearish criteria on 2 closes imply 2 closes below VWAP, which is checked first, so "opposite criteria" can never be
  the recorded reason in v1.
* **Cooldown:** 30 min after INVALIDATED or EXPIRED, per symbol (both sides). **Chop lock:** 2 invalidations within 60
  min → 60 min, per symbol. Caps: 3 per symbol per day, 6 per rolling 60 min (main lane). No new main watch after 15:00.

Confirmed-event sources (read-only): the strategy logs and open-trade state in `data/mias.db`, and the alert outbox
(swing_trend, strong_alignment). Historical days without a forward-paper log use the 4-year replay export
(`data/exports/replay_trades_4yr_…_v2.csv`: B, C, D, Simple entries at their entry minute); E7 / E21 and the alerts
exist only for the days MIAS ran them. Upgrades are reported by source.

## F. Scoring definitions

* Signed return at +5 / 15 / 30 / 60 min: side sign × (close of the last 1-minute bar ending at or before watch + N /
  watch price − 1), in % of the underlying; empty when past 16:00.
* MFE / MAE: best / worst signed 1-minute high / low from the watch to its upgrade (or to its end).
* Lead time: upgrade time − watch time. Price advantage: signed (upgrade price − watch price) / watch price (> 0 = the
  watch saw a better price than the confirmation).
* **Move decomposition — primary reference = the regular-session open (open of the 09:30 1-minute bar):** A = open →
  watch, B = watch → confirmation, C = confirmation → +60 min (signed by the watch side). Consumed by confirmation =
  (A + B) / (A + B + C), pooled over upgraded watches with A + B + C > 0. The same open → entry and entry → +60 min split
  is reported for every confirmed strategy entry, with and without a prior watch.
* Breakdowns: lane, side, structure type, symbol, hour, minutes since open, SPY direction, relative volume, SMA200
  side, RSI, MACD sign, options bias, eventual strategy entry within 60 min (and which), upgrade source; concentration
  (top symbols / days, results without the top symbol / day); suppression counts by reason.

## G. Recommendation criteria (decided now, applied later)

* **ENABLE TELEGRAM PILOT** only if all: Phase A acceptable on ≥ 3 live sessions; ≥ 5 live shadow sessions; main
  watches per day median ≤ 11 and max ≤ 20 (replay and live); upgrade rate ≥ 30%; median lead ≥ 5 min; median price
  advantage > 0; median r15 and r60 > 0; and the signs hold without the top symbol and without the top day.
* **CONTINUE SHADOW MODE** if the replay meets the noise / lead / advantage conditions but the live evidence
  (Phase A, ≥ 5 sessions) is not complete yet.
* **REVISE ARCHITECTURE** if the feed fails Phase A, or noise exceeds the limits, or the median lead is < 2 min.
* **REJECT EARLY WATCH** if in the replay the upgrade rate is < 15% **and** median r60 ≤ 0 **and** median price
  advantage ≤ 0.

The decision to enable Telegram is the user's, separately; nothing is enabled automatically.
