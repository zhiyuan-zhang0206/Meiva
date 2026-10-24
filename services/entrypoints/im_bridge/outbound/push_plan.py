# meiva:note
# The push-failure watchdog's decisions over values: how long to wait before the single retry
# of a send that provably did not start, and whether the user's next message should be told
# that pushes work again.

from __future__ import annotations

__all__ = ["PUSH_RECOVERED_HINT_SECONDS", "recovered_hint_due", "retry_delay_s"]

# meiva:note
# How long after the first successful send a reply may still carry the "recovered" hint.
PUSH_RECOVERED_HINT_SECONDS = 60


# meiva:note
# The wait before a retry: the configured base plus up to `jitter_s` of spread. `draw` is a
# value in [0, 1) the caller drew; the measured failure mode is a connection-establishment
# window of about 0.65 s, which an immediate retry re-enters and a 1 s base with 0 to 2 s of
# jitter walks past.
def retry_delay_s(*, base_s: float, jitter_s: float, draw: float) -> float:
    if not 0.0 <= draw < 1.0:
        raise ValueError("the retry draw must be in [0, 1)")
    return base_s + jitter_s * draw


# meiva:note
# Whether a weixin reply right after a push failure should say the link is back: the adapter
# recorded a recovery and it is at most `window_s` old.
def recovered_hint_due(
    *, recovered_at: float | None, now: float, window_s: float = PUSH_RECOVERED_HINT_SECONDS
) -> bool:
    return recovered_at is not None and now - recovered_at <= window_s
