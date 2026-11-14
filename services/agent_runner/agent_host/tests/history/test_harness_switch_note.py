import json
import logging
from types import SimpleNamespace
from typing import Any, cast

import pytest

from base.agents.messages.timestamp_policy import TimestampSource
from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from services.agent_runner.agent_host.health_routes import prepare_harness_switch_route
from services.agent_runner.agent_host.lifecycle import switch_note

# meiva:note
# Leaving the native `meiva` runtime appends a lifecycle restart note to the outgoing checkpoint
# (logging and continuing on failure); leaving any external harness writes nothing because its CLI
# record owns outgoing history and the destination receives the arrival switch note.


class _RecordingGraph:
    def __init__(self, *, fail: Exception | None = None) -> None:
        self.calls: list[tuple[dict[str, object], dict[str, object]]] = []
        self._fail = fail

    async def aupdate_state(self, config: dict[str, object], values: dict[str, object]) -> None:
        self.calls.append((config, values))
        if self._fail is not None:
            raise self._fail


def _host(*, graph: object | None = None, dropped: list[int] | None = None) -> Any:
    def _drop_agent(agent_id: int) -> None:
        if dropped is not None:
            dropped.append(agent_id)

    return SimpleNamespace(drop_agent=_drop_agent, _graph=graph)


def _route(host: Any) -> Any:
    return prepare_harness_switch_route(
        host,
        timestamps=TimestampSource(
            clock=Clock(ClockConfig("UTC", "UTC", False), timers=SYSTEM_TIMERS),
            timestamps_enabled=True,
        ),
    )


def _payload(agent_id: int, *, old_harness: str = "meiva", new_harness: str = "codex") -> bytes:
    return json.dumps(
        {
            "agent_id": agent_id,
            "old_harness": old_harness,
            "new_harness": new_harness,
            "source": "operator",
            "message": "switch runtime",
        }
    ).encode("utf-8")


async def test_leaving_meiva_appends_switch_note_to_checkpoint_and_drops_agent() -> None:
    graph = _RecordingGraph()
    dropped: list[int] = []
    host = _host(graph=graph, dropped=dropped)

    status_code, body, _content_type = await _route(host)(
        _payload(9211, old_harness="meiva", new_harness="claude_code")
    )

    assert status_code == 200
    assert b'"ok":true' in body
    assert dropped == [9211]
    assert len(graph.calls) == 1
    config, values = graph.calls[0]
    assert config == {"configurable": {"thread_id": "9211"}}
    raw_messages = values["messages"]
    assert isinstance(raw_messages, list)
    messages = cast(list[object], raw_messages)
    assert len(messages) == 1
    assert "Harness switched from 'meiva' to 'claude_code'" in str(
        getattr(messages[0], "content", "")
    )


async def test_checkpoint_failure_logs_warning_and_still_drops_agent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    graph = _RecordingGraph(fail=RuntimeError("checkpoint unavailable"))
    dropped: list[int] = []
    host = _host(graph=graph, dropped=dropped)

    with caplog.at_level(logging.WARNING, logger=switch_note.__name__):
        status_code, body, _content_type = await _route(host)(
            _payload(9212, old_harness="meiva", new_harness="antigravity")
        )

    assert status_code == 200
    assert b'"ok":true' in body
    assert dropped == [9212]
    assert any("failed to record harness switch note" in r.message for r in caplog.records)


@pytest.mark.parametrize("old_harness", ["claude_code", "codex", "antigravity"])
async def test_leaving_external_harness_writes_nothing_and_drops_agent(
    old_harness: str, caplog: pytest.LogCaptureFixture
) -> None:
    graph = _RecordingGraph()
    dropped: list[int] = []
    host = _host(graph=graph, dropped=dropped)

    with caplog.at_level(logging.WARNING, logger=switch_note.__name__):
        status_code, body, _content_type = await _route(host)(
            _payload(9214, old_harness=old_harness, new_harness="meiva")
        )

    assert status_code == 200
    assert b'"ok":true' in body
    assert dropped == [9214]
    assert graph.calls == []
    assert [r for r in caplog.records if r.name == switch_note.__name__] == []


async def test_same_harness_is_a_noop_and_still_drops_agent() -> None:
    graph = _RecordingGraph()
    dropped: list[int] = []
    host = _host(graph=graph, dropped=dropped)

    status_code, body, _content_type = await _route(host)(
        _payload(9215, old_harness="meiva", new_harness="meiva")
    )

    assert status_code == 200
    assert b'"ok":true' in body
    assert dropped == [9215]
    assert graph.calls == []
