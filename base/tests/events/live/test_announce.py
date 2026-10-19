from __future__ import annotations

import ast
import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from base.events.live import announce
from base.events.live.bus import EventBus


def _imported_modules(source: str) -> set[str]:
    # meiva:note
    # Every module name a source imports, including the names it takes from one.
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def test_lifecycle_hints_cannot_reach_the_agent_snapshot() -> None:
    # meiva:note
    # The guard is the module's import list, not a call spy: nothing in `announce`
    # may reach the observation snapshot, so no runtime patch can be forgotten
    # when a code path is added.
    source = Path(announce.__file__).read_text(encoding="utf-8")
    assert not [m for m in _imported_modules(source) if m.startswith("base.agents.observation")]


class _RecordingBus:
    channel = "meiva:test-events"

    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    def publish_best_effort_sync(self, payload: str, *, context: str) -> int:
        self.published.append((payload, context))
        return 0

    async def publish_best_effort(self, payload: str, *, context: str) -> int:
        self.published.append((payload, context))
        return 0


@pytest.mark.parametrize(
    ("publisher", "role"),
    [
        (announce.publish_agent_spawned_sync, "agent_spawned"),
        (announce.publish_agent_updated_sync, "agent_updated"),
    ],
)
def test_sync_lifecycle_hint_has_no_snapshot(
    publisher: Callable[[EventBus, int], None], role: str
) -> None:
    bus = _RecordingBus()
    publisher(cast(EventBus, bus), 7)
    assert len(bus.published) == 1
    payload, context = bus.published[0]
    assert context == role
    assert json.loads(payload) == {"role": role, "agent_id": 7}


async def test_async_lifecycle_hint_needs_no_connection() -> None:
    bus = _RecordingBus()
    await announce.publish_agent_updated(cast(EventBus, bus), 7)
    assert len(bus.published) == 1
    payload, context = bus.published[0]
    assert context == "agent_updated"
    assert json.loads(payload) == {"role": "agent_updated", "agent_id": 7}
