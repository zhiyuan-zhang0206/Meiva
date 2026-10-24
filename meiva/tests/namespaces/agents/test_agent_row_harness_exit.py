from __future__ import annotations

from datetime import UTC, datetime

from base.api_contracts.agent_contract import AgentStatus
from meiva.agents import AgentRow


def test_the_exit_status_pair_defaults_to_none() -> None:
    row = AgentRow(
        agent_id=17,
        label=None,
        status=AgentStatus.RUNNING,
        spawner="user",
        fork_source_agent_id=None,
        machine="macmini",
        spawned_at=datetime(2026, 10, 25, tzinfo=UTC),
        last_active_at=datetime(2026, 10, 25, 7, tzinfo=UTC),
        last_inbound_at=datetime(2026, 10, 25, 6, tzinfo=UTC),
        heartbeat_paused_until=None,
    )
    assert row.harness_exit_code is None
    assert row.harness_exit_at is None


def test_a_clean_exit_stays_zero_and_is_not_the_same_as_absent() -> None:
    row = AgentRow(
        agent_id=17,
        label=None,
        status=AgentStatus.RUNNING,
        spawner="user",
        fork_source_agent_id=None,
        machine="macmini",
        spawned_at=datetime(2026, 10, 25, tzinfo=UTC),
        last_active_at=datetime(2026, 10, 25, 7, tzinfo=UTC),
        last_inbound_at=datetime(2026, 10, 25, 6, tzinfo=UTC),
        heartbeat_paused_until=None,
        harness_exit_code=0,
        harness_exit_at=datetime(2026, 10, 25, 7, 1, tzinfo=UTC),
    )
    assert row.harness_exit_code == 0
    assert row.harness_exit_code is not None
