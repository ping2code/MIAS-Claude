# Early Watch — operational changes (delivery and measurement only)

These are changes to how watches are DELIVERED and MEASURED. None changes a detection rule. v1 (`early-watch-v1`)
and v2 (`early-watch-v2-research`, frozen at 934a2e2) create exactly the same candidates and watches as before.
Historical replays reproduce byte-for-byte.

## 2026-11-12 — Telegram hourly cap removed; delivery and short-horizon measurement added

Requested by the user after the 2026-11-12 session. A valid V2-A Structure watch (NVDA bearish, 13:11 ET) was
detected but not shown, only because of the MIAS v2 Telegram limit of 6 messages per rolling hour.

### Delivery

- **v2 Telegram:** the MIAS limit of 6 messages per rolling hour is removed (`V2Telegram`). The other v2 rules are
  unchanged:
  - main lane (09:45–15:00) only;
  - `telegram_max_age_seconds` (180 s);
  - one message per family / symbol / side / setup;
  - the 5-minute same symbol / side combination.
- **v1 Telegram:** v1 never had a separate delivery cap; every fresh main-lane watch is sent.
  - v1's "6 per rolling hour" is the pre-registered watch-creation rule inside the frozen v1 state machine
    (`rules.max_per_hour`, `Observer._blocked`, reason `hourly_cap`).
  - The user chose to keep the v1 detector frozen, so that rule is unchanged.
  - It is a detection reason (no watch is created). It is never a Telegram suppression.
- **Telegram API safety** (`mias/observer/telegram.py`). These pace delivery and never drop a message for volume:
  - One shared, serialised sender for the v1 and v2 workers.
  - At most 1 message per second to a private chat, and 1 per 3 s to a group chat (Telegram's published limits:
    about 1/s per chat and 20/min per group).
  - A 429 response's `retry_after` is respected for that chat.
  - At most 2 attempts per message.
  - If the required wait is longer than 60 s, the message fails with the reason recorded instead of blocking the
    worker.
  - Per-message worker timeout: 180 s.
  - The bot token is never logged.
- **Unchanged:**
  - stale protection (180 s, including restart / reconnect backfill);
  - duplicate protection (a watch id is sent once; one v2 setup is sent once);
  - opening lane and after-15:00 lane are research only;
  - watch-only messages: no strike, expiry, contract, entry, target, stop or profit expectation;
  - "RESEARCH WATCH ONLY — NOT A TRADE SIGNAL" ends every v2 message;
  - Telegram failures never change watch state.

### Measurement (reports only; `mias/observer/delivery.py`, `fresh.py`)

- **Delivery stages:**
  - RAW CANDIDATE → WATCH CREATED → TELEGRAM ELIGIBLE → TELEGRAM SENT / TELEGRAM FAILED.
  - TELEGRAM SUPPRESSED, with an explicit reason: stale, already sent, combined, outside the Telegram window, or
    other.
  - Replay rows are "not attempted".
  - Historical `hourly cap` rows are kept as recorded and shown as "hourly_cap (historical)". New sessions cannot
    produce one.
- **"Detected but not delivered":** a section in both daily reports listing every created watch that did not reach
  Telegram, with its 5/10/15/30/60-minute signed moves.
- **Short horizons:**
  - Signed stock return at +5/10/15/30/60 min.
  - MFE / MAE over the fixed window [watch, watch + N min].
  - Percent directionally correct and median signed move per family and horizon.
  - The pre-registered MFE / MAE (from the watch until its upgrade or end) is unchanged and still reported.
- **Fresh same-direction setup while a watch is open** (table `fresh_while_open` in both databases):
  - What is logged: the new setup on a NEW leg, the old watch, its remaining lifetime, and whether the new setup
    would otherwise have qualified.
    - v1: the frozen suppression check, read-only.
    - v2: the frozen persistence check run on a private shadow.
  - Its 5–60 minute outcomes are scored in the reports.
  - It never creates, ends or blocks a watch.

No Early Watch rule may be changed on the basis of these measurements without explicit approval.

## 2026-11-15 — Options context attached to every watch (context only)

Requested by the user to answer, for each watch: does current options sentiment support it, conflict with it, or
give no useful confirmation? **Context only.** It never creates, delays, suppresses, upgrades, downgrades or
invalidates a watch. It does not touch any watch rule, lifetime, cap, Telegram policy or EMA / paper strategy.
Verified by replaying 2026-11-12 and 11-13 for v1 and v2 with the old lookup and with the new context: every event
apart from the `context` field is identical, including watch ids and timestamps.

- **Where it comes from:** `mias/observer/optctx.py`.
  - It reads the MIAS cycle `metrics` rows in data/mias.db, read-only, on one persistent connection with a 200 ms
    busy timeout. The database uses WAL.
  - No Massive request is made.
  - The detectors' existing `context(sym, ts)` hook stores it in the watch's `context.optx` at creation time.
- **PCR inputs:**
  - `pcr_volume`: today's put volume ÷ call volume. `pcr_oi`: put OI ÷ call OI.
  - Both cover the MIAS chain scope: expirations 0–100 days out, strikes ±15 % of spot.
  - Same-day (0DTE) expirations are INCLUDED.
  - From 2026-11-16 the snapshot also records the 0DTE part (`call/put_volume_0dte`, `call/put_oi_0dte`), the
    0DTE part of each cycle's flow window, and `top_volume_share`. These are additive fields in `mias/options.py`;
    the options-bias calculation is unchanged.
  - Updated every MIAS cycle (150 s).
- **Freshness:** the snapshot is stale if it is older than 330 s at watch time (2 cycles + 30 s). It is then shown
  as "STALE / INSUFFICIENT DATA".
- **Insufficient data** if any of:
  - options marked thin (fewer than 5,000 contracts today, `thin_options_volume`);
  - quote coverage below 80 %;
  - one contract carrying more than 25 % of today's volume;
  - no flow components yet.
  Rolling PCR additionally needs at least 1,000 contracts and at least 80 % window coverage.
- **Rolling PCR:** 15 and 30 min, current vs previous window, from the per-cycle flow windows.
- **Interpretation:**
  - Flow first: 0.6 × recent + 0.4 × session.
  - PCR direction and skew can only support or contradict the flow direction.
  - Divergences use the detector's own confirmed 5-minute pivots and are weak context only.
  - The classifications: `optctx.py` docstring.
- **Telegram:** one to three compact lines per watch. "Options context: CONFIRMING / CONFLICT / NEUTRAL /
  INSUFFICIENT DATA / STALE / unavailable", then flow / PCR / skew directions, then for single stocks
  stock vs market (SPY + QQQ) options.
  - They are built only from the stored context, so what was shown is what is stored.
  - They never wait for options data.
- **Reports:** v2 report section 12 groups outcomes (5–60 min) by context, by family, Structure on its own, time of
  day, index vs single stock, the market overlay, and divergence. It also gives sample quality per symbol.
  - Watches from before 2026-11-16 are "not recorded".
  - Any reconstruction is labelled "post-hoc reconstructed" and never pooled with live snapshots.
  - Replays keep the old one-field lookup, so historical replays are unchanged.

No Early Watch rule or threshold may be changed on the basis of these statistics without explicit approval.
