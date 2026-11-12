"""MIAS Live Observer + Early Watch (SHADOW MODE).

A separate process (``python -m mias.observer run``, systemd unit ``mias-observer``) that streams completed
1-minute stock bars from the Massive WebSocket (``AM``), keeps a deterministic per-symbol chart state and logs
informational EARLY WATCH events to its own database (``data/observer/observer.db``).

It never trades, never sends Telegram (there is no sender in this package), never writes to the MIAS database and
never calls the strategy engines. Existing MIAS data is only *read* (``mode=ro``) to link watches to the confirmed
events that followed them. Rules are frozen in ``docs/observer/PREREGISTRATION.md`` / ``rules.py``.
"""
