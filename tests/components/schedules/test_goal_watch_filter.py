from __future__ import annotations

import importlib.util
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from base.events.live.projection import EVENT_ADAPTER
from meiva.sdk_surface import process_context
from meiva.tests.fixtures.gateway_wire import (
    GatewayStub,
    GatewayWire,
    Reply,
)
from meiva.tests.fixtures.gateway_wire import (
    gateway_stub as gateway_stub,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SNIPPET_PATH = "meiva_builtins/skills/coordination/meiva-goal/scripts/watch_idle.py"
_FIXTURE_PATH = _REPO_ROOT / "tests" / "fixtures" / "events" / "agent" / "agent_updated.json"


def _load_snippet() -> ModuleType:
    spec = importlib.util.spec_from_file_location("goal_watch_idle", _REPO_ROOT / _SNIPPET_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def is_target_idle() -> Callable[[dict[str, Any], int], bool]:
    fn: Callable[[dict[str, Any], int], bool] = vars(_load_snippet())["_is_target_idle"]
    return fn


@pytest.fixture
def watcher_wire(gateway_stub: GatewayStub) -> Iterator[GatewayStub]:
    previous = process_context.bound_context()
    with GatewayWire.open(gateway_stub, agent_id=7, retries=1) as opened:
        process_context.bind_context(opened.context)
        try:
            yield gateway_stub
        finally:
            if previous is None:
                process_context.unbind_context()
            else:
                process_context.bind_context(previous)


def _agent_updated_event(agent_id: int) -> dict[str, Any]:
    raw = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    raw["agent_id"] = agent_id
    event = EVENT_ADAPTER.validate_python(raw)
    return json.loads(event.model_dump_json())


@pytest.mark.parametrize("status", ["idling", "running", "terminated"])
def test_target_hint_reads_current_status(
    watcher_wire: GatewayStub,
    status: str,
    is_target_idle: Callable[[dict[str, Any], int], bool],
) -> None:
    watcher_wire.reply("GET", "/api/agents/42", {"status": status})
    assert is_target_idle(_agent_updated_event(42), 42) is (status == "idling")
    assert [(r.method, r.path) for r in watcher_wire.received] == [("GET", "/api/agents/42")]


@pytest.mark.parametrize(
    "event",
    [
        {"role": "agent_updated", "agent_id": 99},
        {"role": "llm_done", "agent_id": 42},
    ],
)
def test_other_events_do_not_fetch_status(
    watcher_wire: GatewayStub,
    event: dict[str, Any],
    is_target_idle: Callable[[dict[str, Any], int], bool],
) -> None:
    assert is_target_idle(event, 42) is False
    assert watcher_wire.received == []


def test_poll_fallback_reads_status_until_idle(watcher_wire: GatewayStub) -> None:
    module = _load_snippet()
    watcher_wire.reply_in_turn(
        "GET",
        "/api/agents/42",
        [
            Reply(503, {"detail": "gateway restarting"}),
            Reply(200, {"status": "running"}),
            Reply(200, {"status": "idling"}),
        ],
    )
    watcher_wire.reply("POST", "/api/agents/7/messages", {"status": "accepted"})
    vars(module)["_watch_via_poll"](42, interval_s=0)
    assert [(r.method, r.path) for r in watcher_wire.received] == [
        ("GET", "/api/agents/42"),
        ("GET", "/api/agents/42"),
        ("GET", "/api/agents/42"),
        ("POST", "/api/agents/7/messages"),
    ]
    assert "target agent 42 idled" in watcher_wire.received[-1].body["content"]
