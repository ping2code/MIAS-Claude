# MIAS-Claude: market and options trend alerts

One small Python service that, every 2.5 minutes while the US market is open, tells you over Telegram:

- the **stock trend** of each watched symbol (5m, 15m, 1h and daily, plus a composite),
- the **options trend** for it (flow, put/call, skew, IV, dealer gamma),
- and **when either changes**, agrees or disagrees, or something large prints.

A rewrite of `~/MyRepos/MIAS`, which was not modified.

## Ask the bot anytime

Send these to your Telegram bot. It answers only your configured chat and ignores everyone else.

| Send | You get |
|---|---|
| `NVDA` or `/trend NVDA` (up to 5: `nvda mu amd`) | A fresh read of the symbol, watched or not: swing and intraday trend, options bias, a confidence verdict, IV vs realized volatility, key strikes, and a swing contract to look at |
| `/market` | SPY, QQQ, IWM, watchlist breadth and the sector board |
| `/sector`, `/sector energy` | The sector board, or one sector's members with price, swing, intraday, options and IV |
| `/buy AMZN 11/20 260/280C 6.63 x1` | Track a position (private chat only; first strike = the one you bought; add `by 10/30` for your own exit date). Single options too: `/buy NVDA 12/18 190C 9.85` |
| `/positions`, `/sell AMZN 13.40` | Your positions with live value and P&L; close one (by symbol or `#`, no price = current mid) |
| `/brief` | The day-trade brief (also sent at 09:45): opening read, trend or chop day, candidates, key levels |
| `/scorecard`, `/scorecard 30`, `/scorecard replay` | How the alerts did: share that went the right way 1 hour, 1 day and 5 days later, and the option's return |
| `/ema` | 5-minute EMA 9/21 (SPY, QQQ, NVDA, TSLA, META): where the EMAs are, the last cross and what became of it, today's paper trades |
| `/list`, `/status`, `/help` | Watchlist, service health, how to read the answers |

Example reply (real data, 2026-10-07 close):
```
🟢 NVDA  Wed 23:47 ET
Price 237.70 delayed (-0.64%)
Swing: UP (+54)  1w +68 · 1d +68 · 1h -1
Intraday: NEUTRAL (+12)  1d +68 · 1h -1 · 15m -19 · 5m +32
Options: NEUTRAL (-8)  P/C 0.63 · IV 31.4% · RR25 -3.4
🟡 Confidence MEDIUM: most timeframes UP

IV 29% vs 20d realized 24% → options fair
Levels: call wall 240 · put wall 220 · dealers long γ (pinning)
Swing call to look at: 2026-12-18 235C (72d) Δ0.58 mid 16.43 · spread 0.9% · OI 16,236
```

**Confidence:**
- ✅ **HIGH**: every swing timeframe (1w, 1d, 1h) and the options market point the same way.
- 🟡 **MEDIUM**: the trend agrees but options are neutral or thin, or most timeframes agree with none against.
- ⚪ **LOW**: no clear trend, or the timeframes disagree.
- ⚠️ **CONFLICT**: the trend and options money point opposite ways.

**Options cheap / fair / expensive** compares the ~30-day ATM IV with 20-day realized volatility: below 0.9×
is cheap, above 1.25× is expensive. Buying options is friendlier when they are cheap or fair. When they are
expensive, a debit spread lowers the cost of the IV you pay.

**Swing contract to look at** is the liquid contract closest to Δ0.60 with 50–100 days to expiry, with a spread
under 10% and OI of at least 100. It shows calls when the trend is up and puts when it is down. It is a starting
point for your own judgment, not a recommendation.

**Day trade line**: a separate verdict from the intraday timeframes (5m, 15m, 1h). Only when it is HIGH or
MEDIUM does it show a contract: about Δ0.45, 1–14 days to expiry, spread under 5%, the most liquid strike
nearby. Contracts that expire the same day are excluded by default (set `min_dte = 0` under
`[options.ideas.day]` to allow them). On Stocks Starter the intraday trend is 15 minutes behind, so treat
this line with care until real-time stock data is enabled.

Thin options markets (fewer than 5,000 contracts today, e.g. FICO) are labelled as such. They are left out of
the confidence verdict, and their options flip alerts are suppressed.

## What you receive

Alerts are **tiered** by urgency (`[alerts] mode = "tiered"`, since 2026-10-25):

| Tier | Message | Where, when |
|---|---|---|
| 1 Act now | **Pullback over / bounce over**: swing UP or STRONG UP, intraday dipped to −15 or lower and recovered to +35 (2 checks), options not against it; mirror for downtrends. With swing and day-trade contracts | Group, instantly (max one per symbol per direction per 2 h) |
| 1 Act now | **Strong alignment**, as below | Group, instantly |
| 1 Act now | Both carry the symbol's sector rank, and **⚠️ against market/sector** when SPY/QQQ/IWM or the sector trend the other way | |
| 1 Day trade (paper) | **EMA 9/21 cross + retest** entries (SPY, QQQ, NVDA, TSLA, META) and their management (first target, trail, stop, invalidated, end of day), then a summary at 15:10. See [EMA day trade](#ema-921-day-trade-paper) | Group, instantly |
| 1 Positions | Take-profit (55% of max on spreads, +50% on single options), stop (−50%), underlying within 1.5% of the short strike, time stop (10 trading days or your `by` date), 7 days to expiry, swing trend turned against you. Each once per position | **Private chat of whoever added it** |
| 2 Changes | One message every 15 min (09:45, 10:00, …) if anything changed: swing trend changes, options bias flips, call/put wall breaks, IV spikes, alignment ended, market trend, plus the sector ranking with ▲▼. Only the latest change per symbol shows | Group |
| 3 Context | **Unusual activity** every 5 min: only contracts that traded since the last report, ranked by that new premium, each classified **trade by trade** (buy / sell / mid / multi-leg, sweeps). The first report of the day checks **yesterday's flow against today's open interest**. Blocks ≥ $5M since the last report, judged by their trades. See [Options flow](#options-flow) | Group |
| 3 Context | **Day-trade brief** at 09:45 ([Morning brief](#morning-brief)); **scorecard** every Friday at the close ([Scorecard](#alert-scorecard)) | Group |

**Hysteresis** keeps tier 2 quiet when a score wobbles: a trend counts as UP after passing +30 and stays UP until it
falls back under +15 (STRONG: in at 60, out under 50), confirmed over 2 checks. Options: in at ±50, out inside
±25, confirmed over 3 checks. IV spikes watch the ~30-day ATM IV (+4 points in 30 min). A symbol already alerted in
tier 1 in the last 30 minutes has further tier-1 signals moved to the next batch.

**Tuning (2026-10-26)**: replaying every recorded check from Oct 8 to 23 through the tiered rules:

| Per day (average) | Before | Now |
|---|---|---|
| Tier 1 messages | 10.2 | 10.2 |
| Options flips | 88 | 26 |
| IV spikes (front-expiry IV jumped 3+ points ~300 times a day, mostly 0–2 DTE noise) | 52 | 5 |
| Tier 2 lines after de-duplication (~3 per batch) | 159 | 53 |

Forward moves after replayed tier-1 alerts (12 days, small sample): pullback entries went the right way 64% of
the time to the close and 61% after 3 days (median +0.5%) with the +35 recovery; strong alignment 63% to the
close but only 45% after 3 days, so treat it as an intraday signal, not a swing entry.

All modes:

| Message | When |
|---|---|
| **Strong alignment** (`[alerts] mode = "strong_alignment"` only) | Swing **STRONG UP** + intraday **STRONG UP** + options **BULLISH**, or the bearish mirror. It must hold for 2 checks (5 min), with a 30-minute cooldown per symbol. The alert includes swing and day-trade contract picks. |
| Alignment ended | (strong_alignment mode) When that state breaks (confirmed over 2 checks). Turn off with `alignment_end_alert = false`. |
| **NDX report** | A full NDX reading every 10 minutes from 09:30 to 16:00, with the change since the previous report (`[reports]`) |
| **Daily spread picks** | 10:00 ET (and `/picks` anytime): up to one bull call spread and one bear put spread. Rules: swing UP/STRONG UP (or DOWN), confidence MEDIUM+, options not against, 30–60 days, ~0.50/0.25 delta legs with OI ≥ 1,000. Includes a limit price, breakeven, take-profit (55% of max), a 10-trading-day time stop and a hard exit 7 days before expiry. "No trade today" when nothing qualifies. |
| **Unusual activity** | Every 10 minutes (`[reports] unusual_every_minutes`): the top 10 contracts across the watchlist where today's volume exceeds open interest (≥ $500K premium, no same-day expiries, max 3 per symbol), with 🆕 new / ⬆️ growing markers, volume added since the previous report, and a bullish/bearish lean by last-trade side |
| Digest | 09:50, 11:30, 13:30, 15:30 ET: every symbol's price, swing/intraday/options scores, IV, top unusual contracts |
| Closing summary | At the close (handles early closes) |
| System | Started; degraded after 3 bad cycles in a row; recovered |

**Other alert modes** (`[alerts] mode`): `"all"` (used 2026-10-24) sends every rule: trend and options-bias flips,
aligned/divergence, ≥ $1M blocks, IV spikes, call/put wall breaks and the SPY/QQQ/IWM market trend.
`"strong_alignment"` (used 2026-10-12 to 10-24) sends only the strong-alignment and alignment-ended alerts above.
Switching modes resets the alert state quietly, so a switch never sends a burst of stale alerts.

Example:
```
🟢 PLTR  14:35 ET
• STRONG BULLISH ALIGNMENT: swing STRONG UP · intraday STRONG UP · options BULLISH

Price 198.61 live (+2.31%)
Swing: STRONG UP (+90)  1w +87 · 1d +99 · 1h +76
Intraday: STRONG UP (+66)  ...
Options: BULLISH (+31)  net prem +$4.2M · P/C 0.44 · IV 45.7%
✅ Confidence HIGH: every timeframe UP and options bullish
Swing call: ... · Day trade call: ...
```

## Options flow

The unusual-activity report separates new activity from the day's totals and reads every trade:

- **New, not cumulative**: a contract shows only if it traded since the previous report, ranked by the premium
  traded in that window. The day's total, volume and open interest follow on the same line.
- **Trade by trade**: each print in the window is compared with the quote in force when it printed. Buyer-initiated
  (at or near the ask), seller-initiated (at or near the bid), mid (no side), and **multi-leg** (the exchange flags
  spreads, rolls and combos; stock-tied trades too) are totalled separately; sweeps are counted. A contract is
  called bullish or bearish only when 60%+ of its premium is one way. Before, the whole contract took the side of
  its last trade.
- **Opening or closing?** Volume above open interest doesn't prove new positions: intraday volume includes closing
  trades and the same contracts changing hands. Open interest is published once a day, so the first report each
  morning checks yesterday's flagged contracts: ✅ opened (OI rose by half the volume or more), ↔️ partly, ✖️
  closing/turnover.
- Costs two API calls per contract for the top 30 contracts of each window (about 1–2 seconds).

Example (live, 2026-10-26):
```
⬆️ 🟢 META 11/20 660C · new $8.8M (+4,193) · buy 97% · sell 2% · mid 1% · 17 sweeps · day $22.2M, vol 10,751 vs OI 3,382
⬆️ ⚪ QQQ 11/20 714P · new $3.2M (+2,011) · multi-leg 99% · day $6.7M, vol 4,138 vs OI 2,372
⬆️ 🔴 TSLA 10/30 400C · new $372K (+656) · buy 20% · sell 66% · mid 9% · multi-leg 5% · 2 sweeps · ...
New flow (trade by trade): bullish $10.1M · bearish $1.6M · no side $480K · multi-leg $3.3M
```

## Contract choice: IV and earnings

Under every swing contract (`/trend`, pullback and strong-alignment alerts):

- **IV state** (30-day ATM IV against 20-day realized volatility, and its percentile once 20+ days are recorded):
  *fair* is where the 2-year backtest made its money; *expensive* (1.25× realized or top 30% of its history) and
  *below realized* (the stock already swinging hard) both did worse, for single options and debit spreads alike.
  So the advice is to size down or wait, not to switch structures. The debit spread (~0.60Δ long, ~0.30Δ short)
  is shown as the cheaper way in.
- **Earnings** (estimated; the data plan has no earnings calendar): earnings show up as a gap or big move on 2×+
  normal volume about every 91 days, so the next report is estimated from the last one (±1 week; "rough" when
  the quarterly rhythm isn't confirmed). A warning appears when it falls before the expiry, and a stronger one
  when it is within a week. The daily picks skip symbols reporting within 5 trading days and warn about later ones.
- **Events priced in options**: an expiry whose IV implies a forward volatility far above the rest of the curve has
  an event before it (earnings, a ruling, a launch). The warning gives the implied move.

## Alert scorecard

Every directional alert is logged (pullback entries, strong alignment, trend changes, options flips, wall breaks,
EMA entries, one-sided flow, flow confirmed by open interest) and scored 1 hour (capped at the close), 1 trading
day and 5 trading days later: did the stock move the alert's way, and what did the named contract return.
`/scorecard` (7 days), `/scorecard 30`, every Friday at the close, and `python -m mias scorecard`.
`python -m mias scorecard --backfill` replays the recorded checks through today's rules; `/scorecard replay`
shows that history.

## Morning brief

At 09:45 ET (`[brief]`, and `/brief` anytime):
- SPY, QQQ, IWM: gap, first 15 minutes, VWAP side, opening volume against normal
- the day's character so far: **trending** (ADX ≥ 25 and a real move from the open) or **choppy**
- the leading sector
- **3–5 day-trade candidates**: watchlist stocks with liquid short-dated options and a real opening move (gap or
  first-15-minute move ≥ 0.5%), ranked by relative opening volume × the move, each with its opening range, VWAP,
  prior-day high/low/close and a day-trade contract (1–14 days, ~0.45Δ, bid/ask spread ≤ 5%)
- key levels for SPY and QQQ, with the call and put walls

## Swing entries backtest

`python -m mias pullback-backtest`: 55 symbols, Oct 2024 to Oct 2026, scores rebuilt every 30 minutes from
5-minute and daily bars, each entry bought at the next hourly option bar (real prices), exits as `[positions]`
(+50% / 55% of max, −50%, 10 trading days). No options-flow filter (no history that far back). Profit factor:

| Entry | Calls, single | Calls, spread | Puts, single | Puts, spread |
|---|---|---|---|---|
| Strong alignment (swing and intraday ≥ 60) | **1.26** | **1.27** | 0.61 | 0.60 |
| Buy the trend (every 5th day while swing UP) | 1.16 | 1.21 | 0.70 | 0.75 |
| Pullback over (the tier-1 rule) | 1.09 | 1.19 | 0.62 | 0.60 |

| Calls by IV at entry | Strong, single | Pullback, single | Pullback, spread |
|---|---|---|---|
| IV below realized | 0.95 | 0.89 | 1.14 |
| IV fair | **1.82** | 1.20 | 1.43 |
| IV expensive | 0.91 | 1.05 | 0.89 |

What it says: the period rose, so calls made money and puts lost almost everywhere; the pullback rule did not beat
simpler entries over two years (the 12-day replay favoured it, too small a sample); fair IV mattered more than
the entry rule; spreads did not rescue expensive-IV entries; exiting when the swing trend turned against the trade
didn't help. Trades held through earnings looked better, but the earnings dates are found from big moves after
the fact, so that split is biased and isn't used. Full tables in `data/pullback_backtest/report.md`.

## Day-trade setups backtest

`python -m mias daytrade-backtest`: SPY, QQQ, NVDA, TSLA, META, Apr 27 to Oct 23 2026, 5 contracts, the handout's
management, real option minute bars, 1% slippage on every fill.

| Setup | Trades | Win | Profit factor | P&L |
|---|---|---|---|---|
| EMA 9/21 cross + retest, 2-minute candles | 1,265 | 33% | 0.80 | −$36,024 |
| EMA 9/21, 3-minute | 947 | 31% | 0.64 | −$56,713 |
| EMA 9/21, 5-minute | 554 | 30% | 0.55 | −$44,766 |
| Opening-range breakout | 242 | 37% | 0.71 | −$13,530 |
| VWAP reclaim / lost | 940 | 36% | 0.80 | −$32,542 |
| VWAP rejection | 606 | 30% | 0.50 | −$53,112 |
| Prior-day high/low break | 254 | 40% | 0.70 | −$13,993 |

None made money; the trend-day filter applied to few trades and didn't change that. So no setup gets live
alerts; the EMA paper trades continue as a live test. Per-symbol tables in `data/daytrade_backtest/report.md`.

## EMA 9/21 day trade (paper)

The 5-minute EMA 9/21 cross-and-retest from the SPY handout, run on SPY, QQQ, NVDA, TSLA and META (`[daytrade]`, `mias/emacross.py`):

1. EMA 9 crosses EMA 21 on a completed 5-minute candle.
2. Within 6 candles price pulls back into the EMAs (bull: low touches EMA 9) and holds (closes stay above EMA 21).
   A close through EMA 21 or an opposite cross cancels it; no pullback means no trade (no chase).
3. A green candle closing above EMA 9 and the prior close is the entry. Invalidation: the retest's lowest low.

Management: ATM 0DTE (1DTE from 13:00), 5 contracts, stop −20% or a 5-minute close through the invalidation, sell 3
at +30% and move the runners to breakeven, at +75% protect +50%, at +100% protect +75%, close all by 15:10 ET, no new
entries after 2 losses in a row. Puts mirror it. Times are ET.

The service paper-trades it on live option quotes and alerts the group at each step.

How the paper trades are kept honest and studied:
- **Real fills**: bought at the ask, sold at the bid (never above the trigger price). The midpoint result is shown
  next to it. On 2026-10-26 the three trades made +$235 at mid but +$110 at bid/ask (META's 4.4% spread alone
  cost ~$80).
- **Checked every 30 seconds** (`manage_seconds`) while a paper trade is open, not just every 150-second cycle,
  so stops and trails are acted on close to where they trigger.
- **Recorded for study** (`python -m mias ema-log`, CSV in `data/ema_log.csv`): entry reason (cross, pullback,
  confirmation candle), the confirmed candle-close time and the fill time (delay in seconds), the chart stop and
  the premium stop each as a % stock move (the −20% premium stop is often several times tighter than the chart
  stop on same-day options), bid/ask spread at entry and exit, MFE and MAE (best and worst option price, with the
  time, and the stock's best and worst move), and the swing and intraday trend at entry.
- **Swing-trend check**: each entry alert says whether it goes with or against the swing trend (META's losing
  call on 10/26 went against a swing DOWN). `swing_filter = true` skips entries against it. It stays off:
  in the 6-month backtest, trading only with the swing trend didn't rescue any setup (EMA 5m: with the trend PF
  0.49, against 0.70; EMA 2m: with 0.88, against 0.59; VWAP reclaim with 0.91). Every split still lost money.

**Backtest** (`python -m mias ema-backtest`, real SPY 5-minute bars and the real option minute bars, Apr 27 to
Oct 23 2026, no commissions or slippage):

| | Trades | Win | P&L (5 contracts) | Profit factor |
|---|---|---|---|---|
| SPY, handout rules | 116 | 30% | −$4,486 | 0.63 |
| SPY, control: enter on the cross | 207 | 38% | −$2,315 | 0.86 |
| QQQ, handout rules | 113 | 35% | −$1,684 | 0.88 |
| NVDA, handout rules (weekly expiry) | 104 | 32% | −$2,574 | 0.74 |
| TSLA, handout rules | 111 | 30% | −$9,653 | 0.58 |
| META, handout rules | 111 | 34% | −$9,801 | 0.67 |

No variant tried (stop 35% or 50%, target +50%, 0DTE all day, mornings only, a 3-candle retest) made money on SPY.
VWAP and opening-range alignment made it worse, not better. Only 29% of crosses moved $2 their way before the next
opposite cross (median $0.96). Read the live alerts as a test, not as signals. The report, the trade list and a CSV
are written to `data/ema_backtest/`.

## Sectors

Four sectors that tend to rotate are configured under `[sectors]` in `config.toml`, each with an ETF as its gauge.
Members and ETFs join the watchlist automatically.

| Sector | Members | Gauge |
|---|---|---|
| Tech & Semis | NVDA, MU, AMD, AVGO, META, MSFT, AMZN, GOOGL, AAPL, PLTR | SMH |
| Financials | GS, JPM, V, HOOD, COIN | XLF |
| Energy | XOM, CVX, OXY, USO | XLE |
| Defensive | LLY, UNH, COST, WMT, PG | XLV |

The **sector board** ranks them by the members' average swing score, with intraday score, how many members are up
and down, the ETF's score and the leading member (the strongest in an up sector, the weakest in a down one), and
ends with a "Focus" line naming the strongest sector. It heads every digest (09:50, 11:30, 13:30, 15:30 ET) and the
closing summary, and is in `/market` and `/sector`.

```
🟢 Energy STRONG UP (+90) · intraday +66 · 4↑ 0↓ · XLE +82 · lead CVX +98
🟢 Defensive UP (+56) · intraday +1 · 5↑ 0↓ · XLV +68 · lead LLY +62
⚪ Tech & Semis NEUTRAL (-10) · intraday -45 · 3↑ 6↓ · SMH -58 · lead AVGO -72
⚪ Financials NEUTRAL (-15) · intraday -54 · 1↑ 3↓ · XLF +42 · lead GS -68
Focus: Energy · weakest: Financials
```

## NDX (and other index-style symbols)

The plan has real-time NDX **options** but not real-time NDX index bars or snapshots. `[instruments.NDX]` therefore:
- reads the trend from **QQQ** bars, which track NDX almost exactly (the scores don't depend on price level),
- takes the **live price** from NDX option quotes (put-call parity) and the previous close from NDX daily bars,
- builds the options read (flow, P/C, IV, skew, walls, contracts) from the **NDX chain** (`I:NDX`, ±6% strikes).

NDX contracts are large (a 0.6-delta swing call is about $120,000), so treat NDX mainly as a market read; QQQ
options are the practical way to trade it.

## How the scores work

**Profiles.** The headline trend and the trend-change alerts use the **swing** profile (1w 35%, 1d 45%,
1h 20%), which suits holding options for weeks. **Intraday** (5m 25%, 15m 30%, 1h 30%, 1d 15%) is shown
alongside. Switch with `trend.profile` in `config.toml`.

**Stock trend** (−100 … +100 per timeframe): EMA 9/21/50 ordering, 20-bar regression slope in ATRs, RSI distance
from 50, higher highs and higher lows, and distance from session VWAP. The sum is scaled by ADX, so weak trends
score lower. 15m and 1h bars are built from regular-session 5-minute bars aligned to 09:30, and weekly bars
from daily bars. Labels: ≥ 60 strong up, ≥ 25 up, −25…25 neutral, and the mirror image down.

**Options bias** (−100 … +100), from the chain snapshot each cycle (expiries ≤ 100 days, strikes ± 15%):
- *Session flow* (35%): premium added since the last cycle, per contract. It is signed by whether the last print
  was at the ask (bought) or the bid (sold). Calls bought and puts sold count as bullish; puts bought and calls
  sold count as bearish.
- *Recent flow* (30%): delta-weighted version of the same, over the last 15 minutes.
- *Put/call* (15%): today's volume P/C compared with the open-interest P/C.
- *Skew* (20%): change in the 25-delta risk reversal since the first reading of the day.

Also reported: ATM IV (front and ~30-day), dealer gamma exposure, call wall and put wall (largest OI above and
below the price), and contracts trading above their open interest.

**Live price.** With Stocks Advanced (current plan, `stock_delay_minutes = 0`) the price and all bars are real
time. If you ever go back to Stocks Starter, set `stock_delay_minutes = 15`. The price is then estimated from
put-call parity on the nearest expiry (S ≈ K + Call − Put, median of the 4 nearest strikes) and still marked
`live`, while the bars lag 15 minutes.

**Limits to know:**
- Buy/sell side is an estimate: a snapshot shows only the *latest* trade per contract.
- Flow history starts when the service starts each day.
- Same-day (0DTE) contracts count in flow, but not in the unusual and block alerts (`alert_0dte`).

## Commands

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

```bash
.venv/bin/python -m mias once                  # one cycle now, prints the digest, changes nothing
.venv/bin/python -m mias once --send           # ... and sends it to Telegram
.venv/bin/python -m mias rank --top 20         # rank ~70 candidates by options premium traded
.venv/bin/python -m mias telegram-test
.venv/bin/python -m mias ema-backtest --symbols SPY,QQQ --start 2026-04-27 --end 2026-10-23
.venv/bin/python -m mias daytrade-backtest     # EMA (2/3/5m), ORB, VWAP reclaim/reject, prior-day breaks
.venv/bin/python -m mias pullback-backtest     # swing entries over 2 years on real option prices
.venv/bin/python -m mias scorecard --backfill  # replay recorded checks into the scorecard
.venv/bin/python -m mias run                   # the service
.venv/bin/python -m pytest -q
```

Secrets come from the environment or a `.env` file next to `config.toml` (mode 0600, git-ignored):
`MASSIVE_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

The service has a dashboard at `http://localhost:8080/` (auto-refreshes; no login, so keep it private).
`/status` returns JSON and `/healthz` is the health check.

**Watchlist:** your 15 focus symbols plus SPY. `/trend` works for any symbol. Use `rank` to compare candidates
by options activity.

## Robustness

- Each symbol is processed independently. A failing symbol or request is retried with backoff, then reported as
  partial; the other symbols and the next cycle carry on. There is no global halt.
- Each cycle has a timeout of 90% of the interval, so cycles never pile up.
- Alerts are written to SQLite before sending. If Telegram is down, they are retried for up to an hour, and
  nothing is lost on restart.
- Alert state (confirmed trends, cooldowns, flow baselines) is persisted, so restarts don't re-alert.
- Market hours come from Massive's market-status endpoint (holidays, early closes), with the clock as fallback.
- After 3 bad cycles in a row you get a Telegram "degraded" message, and a "recovered" message when data flows
  again. `/healthz` drives container restarts.
- The API key is sent only in the `Authorization` header and never appears in logs or errors. Pagination is
  restricted to the Massive host.

## Where to run it

The service is tiny: about 150 API calls per cycle, under 5 s per cycle, under 150 MB of RAM. **No new hardware
is needed.** Pick by reliability:

1. **Your OpenShift cluster** (recommended if it is up during market hours). See below. It is one pod with a 2 GiB
   volume and no publisher, `oc exec` or batch pipeline, so the failure modes behind the old MIAS halts don't exist.
2. **A small cloud VM** in a US-East region (1 vCPU, 1–2 GB RAM, the cheapest tier is enough), running
   `podman compose up -d` or the systemd unit. It is the best choice if the cluster is not reliably available.
3. **This WSL machine** with `deploy/systemd/mias-claude.service`. It is fine for trying it out, but alerts stop
   whenever Windows sleeps, updates or reboots.

### Deploy on OpenShift

```bash
oc project <namespace>
oc create secret generic mias-secrets --from-env-file=.env
oc create configmap mias-config --from-file=config.toml
oc new-build --binary --strategy=docker --name=mias
oc start-build mias --from-dir=. --follow
sed "s/REPLACE_NAMESPACE/$(oc project -q)/" deploy/openshift/mias.yaml | oc apply -f -
oc logs -f deploy/mias
oc port-forward svc/mias 8080:8080
```

After editing `config.toml`:

```bash
oc create configmap mias-config --from-file=config.toml --dry-run=client -o yaml | oc apply -f - && oc rollout restart deploy/mias
```

## Layout

```
mias/massive.py     API client (retries, pagination guard)
mias/trend.py       stock trend per timeframe + composite      mias/indicators.py  EMA/RSI/ATR/ADX/VWAP
mias/options.py     chain analytics, implied spot, flow, bias
mias/signals.py     alert rules, confirmation, cooldowns       mias/formatting.py  Telegram text
mias/engine.py      one cycle across all symbols               mias/app.py         scheduler, health
mias/store.py       SQLite (state, history, alert outbox)      mias/web.py         dashboard
mias/bot.py         Telegram commands (/trend, /market)        mias/verdict.py     confidence verdict
mias/emacross.py    EMA 9/21 day trade: detection, backtest, live paper trades
mias/daysetups.py   other intraday setups (ORB, VWAP, prior-day breaks) and their comparison backtest
mias/swingtest.py   swing entries backtest (pullback vs strong vs trend, single vs spread)
mias/flow.py        trade-by-trade flow classification, next-day open-interest check
mias/events.py      earnings estimate, IV events, IV rank, single-vs-spread advice
mias/scorecard.py   alert outcomes 1h/1d/5d          mias/brief.py   09:45 day-trade brief
```
