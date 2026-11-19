"""V3 EARLY WATCH — a separate live research / alert pipeline (added 2026-11-18).

Detection is a clone of the current V2 (structure / VWAP / opening range, same values, early_watch_v3.toml); V3 has
its own state, config, presentation, Telegram chat and queue, storage and reports, and runs as its own process
(``python -m mias.early_watch_v3 run``, user unit ``mias-early-watch-v3``). V1 and V2 (mias.observer) are frozen
baselines: they never import this package, and V3 only reads their data (read-only). Research only: WATCH ONLY —
NOT A TRADE SIGNAL; no contract recommendations.
"""
