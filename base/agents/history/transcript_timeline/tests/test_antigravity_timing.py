from __future__ import annotations

import json
from pathlib import Path

from base.agents.history.transcript_timeline.items import format_tool_use
from base.agents.history.transcript_timeline.record.harness import record_file
from base.agents.history.transcript_timeline.record.read import reasoning_refresh
from base.agents.history.transcript_timeline.tests.support import POLICY, window
from base.lm.harness.antigravity.timing_store import store_timings, timing_path
from base.lm.harness.antigravity.trajectory import parse_trajectory
from base.lm.harness.session.transcript.record_file import UnreadRows


def test_native_duration_revises_only_same_reasoning_id_and_survives_cold_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "transcript_full.jsonl"
    row = {
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "step_index": 0,
        "created_at": "2026-11-17T05:30:00Z",
        "thinking": "synthetic thought",
        "content": "MEIVA_DISPLAY_TO_USER\nanswer",
        "tool_calls": [{"name": "run_command", "args": {"CommandLine": "echo fixture"}}],
    }
    path.write_text(json.dumps(row) + "\n")
    file = record_file("antigravity", path)
    before = window(file, harness="antigravity").timeline_items
    reasoning = next(item for item in before if item.kind == "agent_reasoning")
    assert reasoning.reasoning_ms is None
    assert any(item.kind == "agent_chat" for item in before)
    native = parse_trajectory(
        {
            "status": "CASCADE_RUN_STATUS_IDLE",
            "numTotalSteps": 1,
            "trajectory": {
                "steps": [
                    {
                        "type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE",
                        "status": "CORTEX_STEP_STATUS_DONE",
                        "metadata": {"createdAt": "2026-11-17T05:30:00.123456789Z"},
                        "plannerResponse": {
                            "thinking": "synthetic thought",
                            "thinkingDuration": "30.265829s",
                        },
                    }
                ]
            },
        }
    )
    revision = file.revision()
    store_timings(path, native)
    assert file.revision() != revision
    refresh = reasoning_refresh(
        file,
        harness="antigravity",
        start=0,
        policy=POLICY,
        render_tool=format_tool_use,
        unread=UnreadRows(),
    )
    assert len(refresh.items) == 1
    item = refresh.items[0].item
    assert item.item_id == reasoning.item_id
    assert item.kind == "agent_reasoning" and item.reasoning_ms == 30265
    assert refresh.turns_closed == 0 and not refresh.pending
    reloaded = window(record_file("antigravity", path), harness="antigravity").timeline_items
    assert [(i.item_id, i.reasoning_ms) for i in reloaded] == [
        (i.item_id, 30265 if i.kind == "agent_reasoning" else i.reasoning_ms) for i in before
    ]
    timing_path(path).unlink()
    lost = reasoning_refresh(
        file,
        harness="antigravity",
        start=0,
        policy=POLICY,
        render_tool=format_tool_use,
        unread=UnreadRows(),
    )
    assert lost.items[0].item.item_id == reasoning.item_id
    assert lost.items[0].item.reasoning_ms is None
    assert record_file("codex", path).revision() == 0
