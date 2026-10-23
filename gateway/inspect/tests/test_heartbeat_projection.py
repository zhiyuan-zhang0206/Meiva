from datetime import UTC, datetime, timedelta

from base.api_contracts.agent_contract import AgentStatus
from base.config import settings
from gateway.inspect import _live


def test_zero_jitter_span_disables_jitter() -> None:
    last_active_at = datetime(2026, 1, 1, tzinfo=UTC)
    heartbeat = _live.project_heartbeat(
        AgentStatus.IDLING,
        last_active_at,
        None,
        None,
        agent_id=17,
        pending_inbound=False,
        last_pause=None,
        jitter_span_s=0,
    )
    assert heartbeat.paused_until is None
    assert heartbeat.heartbeat_pending is False
    assert heartbeat.next_at == last_active_at + timedelta(
        seconds=settings.daemon.heartbeat_idle_threshold_seconds
    )
