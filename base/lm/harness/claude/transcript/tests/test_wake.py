from __future__ import annotations

import asyncio

from base.lm.harness.claude.transcript.wake import WakeSessionStore


def test_append_only_wakes_with_the_subpath_and_load_never_returns_rows() -> None:
    woken: list[str | None] = []
    store = WakeSessionStore(woken.append)
    asyncio.run(store.append({"project_key": "p", "session_id": "s"}, [{"type": "user"}]))
    asyncio.run(
        store.append({"project_key": "p", "session_id": "s", "subpath": "subagents/agent-a"}, [])
    )
    assert woken == [None, "subagents/agent-a"]
    assert asyncio.run(store.load({"project_key": "p", "session_id": "s"})) is None
