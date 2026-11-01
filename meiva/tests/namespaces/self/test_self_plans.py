from __future__ import annotations

import json

import pytest

from meiva.self.plans import (
    SqlStatement,
    compact_statement,
    heartbeat_pause_statements,
    pause_duration,
    restart_statements,
    terminate_statement,
)


@pytest.mark.parametrize("overlay", [None, {}])
def test_a_restart_without_an_overlay_writes_only_the_restart_inbound(
    overlay: dict[str, object] | None,
) -> None:
    (insert,) = restart_statements(41, "restarting", overlay)
    assert "'restart', 'self'" in insert.sql
    assert insert.params == (41, "restarting", None)


def test_a_restart_with_an_overlay_merges_it_then_queues_the_restart() -> None:
    overlay: dict[str, object] = {"llm_model": "model-a", "effort": "high"}
    merge, insert = restart_statements(41, "restarting", overlay)
    assert merge.sql.startswith("UPDATE agents_meta SET config_overlay")
    assert merge.params == (json.dumps(overlay), 41)
    assert "'restart', 'self'" in insert.sql
    assert insert.params == (
        41,
        "restarting",
        '{"config_overlay": {"effort": "high", "llm_model": "model-a"}}',
    )


def test_restart_statements_leave_the_overlay_untouched() -> None:
    overlay: dict[str, object] = {"llm_model": "model-a"}
    restart_statements(41, "restarting", overlay)
    assert overlay == {"llm_model": "model-a"}


def test_a_terminate_writes_one_self_sourced_inbound_for_the_target() -> None:
    statement = terminate_statement(7)
    assert "'terminate', 'self'" in statement.sql
    assert statement.params == (7,)


def test_a_compact_writes_the_summary_inbound_for_the_agent() -> None:
    statement = compact_statement(41, "Requests: none")
    assert "'compact_summary'" in statement.sql
    assert statement.params == (41, "Requests: none")


def test_a_pause_is_logged_then_applied_to_the_agent() -> None:
    log, apply = heartbeat_pause_statements(41, 90.0)
    assert log == SqlStatement(
        "INSERT INTO heartbeat_pause_log (agent_id, duration_s) VALUES (%s, %s)", (41, 90.0)
    )
    assert apply.sql.startswith("UPDATE agents_meta SET heartbeat_paused_until")
    assert apply.params == (90.0, 41)


@pytest.mark.parametrize(("duration", "limit"), [(1, 86400.0), (90.5, 86400.0), (86400, 86400.0)])
def test_a_pause_within_the_limit_is_accepted_as_seconds(duration: float, limit: float) -> None:
    seconds = pause_duration(duration, limit)
    assert seconds == duration
    assert isinstance(seconds, float)


@pytest.mark.parametrize("duration", [0, -5, 0.0, float("nan")])
def test_a_pause_must_be_positive(duration: float) -> None:
    with pytest.raises(ValueError, match="greater than 0 seconds"):
        pause_duration(duration, 86400.0)


def test_a_pause_above_the_limit_names_the_limit_and_how_to_change_it() -> None:
    with pytest.raises(
        ValueError, match=r"at most 3600 seconds .*MEIVA_HEARTBEAT_PAUSE_MAX_SECONDS"
    ):
        pause_duration(7200, 3600.0)
