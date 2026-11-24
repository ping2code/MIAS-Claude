"""V3 Telegram delivery policy (pure; the service sends). Pacing / safety copied from V2 (mias.observer.v2.V2Telegram):
main lane only, the maximum age (no restart-backfill or stale sends), one message per family / symbol / side / setup,
and one combined message per symbol / side within combine_seconds. Each family has a send flag in early_watch_v3.toml
(a family switched off is recorded as 'muted' and leaves the policy state untouched, as in V2).

Confluence policy (2026-11-24, ``min_families``): a Watch is Telegram-eligible only when at least ``min_families``
distinct families agree on it, i.e. are open on the same symbol / side when it is created (the families its message
shows; the presentation already lists them). Below that it is recorded as 'muted' with the reason
``v3_single_family_muted: <family>`` and, like a family send flag, leaves the policy state untouched, so a second family
joining later still sends ONE combined message; a third family inside combine_seconds is 'merged' into it (no second
message). ``min_families = 1`` is the launch behaviour (every family sends). Delivery only: nothing here changes
detection, Watch creation or storage.

``v2_would_send`` tags each V3 Watch with what V2's current policy (its own V2Telegram class and its current muted
families, read from config.toml without changing it) would have done with the same Watch."""
from __future__ import annotations


NAME = {"structure": "structure", "vwap": "vwap", "or": "opening_range"}
ORDER = ("structure", "vwap", "or")


class V3Telegram:
    def __init__(self, combine_s: int = 300, max_age_s: float = 180, send: dict | None = None, min_families: int = 1):
        self.combine_ms, self.max_age_ms = combine_s * 1000, max_age_s * 1000
        self.send_flags = dict(send or {})
        self.min_families = max(1, int(min_families))
        self.sent_setups: set = set()
        self.recent: dict[tuple, tuple[int, str]] = {}

    @staticmethod
    def setup(w: dict) -> str:
        return f"OR{w['day']}" if w["family"] == "or" else w["leg_id"]

    def decide(self, w: dict, now_ms: int, families=None) -> tuple[str, str | None]:
        """('send', group) | ('merged', group) | ('muted', reason) | ('skip', reason). ``families``: the families
        open on this symbol / side when the Watch was created (its message's family list); None = its own only."""
        if w["lane"] != "main":
            return "skip", f"lane {w['lane']}"
        if not self.send_flags.get(w["family"], True):
            return "muted", f"{w['family']}_send_off"
        fams = [f for f in ORDER if f in set(families or ()) | {w["family"]}]
        if len(fams) < self.min_families:
            if len(fams) == 1:
                return "muted", f"v3_single_family_muted: {NAME[w['family']]}"
            return "muted", f"v3_below_min_families: {' + '.join(NAME[f] for f in fams)}"
        if now_ms - w["watch_at"] > self.max_age_ms:
            return "skip", "found late"
        key = (w["family"], w["sym"], w["side"], self.setup(w))
        if key in self.sent_setups:
            return "skip", "setup already sent"
        last = self.recent.get((w["sym"], w["side"]))
        if last and w["watch_at"] - last[0] <= self.combine_ms:
            self.sent_setups.add(key)
            return "merged", last[1]
        group = w["watch_id"]
        self.sent_setups.add(key)
        self.recent[(w["sym"], w["side"])] = (w["watch_at"], group)
        return "send", group


def v2_policy(config_toml: str = "config.toml", max_age_s: float | None = None):
    """A private instance of V2's own policy class with V2's current settings (read-only)."""
    from ..observer import settings as obs_settings
    from ..observer.v2 import V2Telegram
    s = obs_settings.load(config_toml)
    return V2Telegram(max_age_s=s.telegram_max_age_s if max_age_s is None else max_age_s,
                      muted=s.v2_telegram_muted), s.v2_telegram_muted
