# meiva:note
# The per-session touch throttle: one database write per interval, bounded bookkeeping.

from __future__ import annotations

from gateway.http.auth.session_store import SessionTouchThrottle

_TTL_S = 3600.0


def test_a_session_is_touched_once_per_interval() -> None:
    throttle = SessionTouchThrottle()
    start = 1_000.0
    assert throttle.due("s", start, stale_after_s=_TTL_S)
    assert not throttle.due("s", start + SessionTouchThrottle.INTERVAL_S - 1, stale_after_s=_TTL_S)
    assert throttle.due("s", start + SessionTouchThrottle.INTERVAL_S, stale_after_s=_TTL_S)


def test_bookkeeping_drops_expired_sessions_and_caps_the_rest_by_oldest_touch() -> None:
    now = 10_000.0
    throttle = SessionTouchThrottle()
    throttle.last_touch["stale"] = now - _TTL_S - 1
    throttle.last_touch.update(
        {f"recent-{index}": now - index for index in range(SessionTouchThrottle.MAX_ENTRIES + 1)}
    )

    assert throttle.due("token", now, stale_after_s=_TTL_S)

    assert "stale" not in throttle.last_touch
    assert "token" in throttle.last_touch
    assert len(throttle.last_touch) == SessionTouchThrottle.MAX_ENTRIES
