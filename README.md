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
| `/market` | SPY, QQQ, IWM and watchlist breadth |
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

| Message | When |
|---|---|
| **Strong alignment** (default alert mode) | Swing **STRONG UP** + intraday **STRONG UP** + options **BULLISH**, or the bearish mirror. It must hold for 2 checks (5 min), with a 30-minute cooldown per symbol. The alert includes swing and day-trade contract picks. |
| Alignment ended | When that state breaks (confirmed over 2 checks). Turn off with `alignment_end_alert = false`. |
| **NDX report** | A full NDX reading every 10 minutes from 09:30 to 16:00, with the change since the previous report (`[reports]`) |
| **Daily spread picks** | 10:00 ET (and `/picks` anytime): up to one bull call spread and one bear put spread. Rules: swing UP/STRONG UP (or DOWN), confidence MEDIUM+, options not against, 30–60 days, ~0.50/0.25 delta legs with OI ≥ 1,000. Includes a limit price, breakeven, take-profit (55% of max), a 10-trading-day time stop and a hard exit 7 days before expiry. "No trade today" when nothing qualifies. |
| **Unusual activity** | Every 10 minutes (`[reports] unusual_every_minutes`): the top 10 contracts across the watchlist where today's volume exceeds open interest (≥ $500K premium, no same-day expiries, max 3 per symbol), with 🆕 new / ⬆️ growing markers, volume added since the previous report, and a bullish/bearish lean by last-trade side |
| Digest | 09:50, 11:30, 13:30, 15:30 ET: every symbol's price, swing/intraday/options scores, IV, top unusual contracts |
| Closing summary | At the close (handles early closes) |
| System | Started; degraded after 3 bad cycles in a row; recovered |

Set `[alerts] mode = "all"` to restore every rule: trend and options-bias flips, aligned/divergence, ≥ $1M blocks,
IV spikes, call/put wall breaks and the SPY/QQQ/IWM market trend. Checkpoint `checkpoint-2026-10-12` is the last
version where that was the default.

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
```
