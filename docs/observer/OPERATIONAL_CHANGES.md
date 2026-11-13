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
