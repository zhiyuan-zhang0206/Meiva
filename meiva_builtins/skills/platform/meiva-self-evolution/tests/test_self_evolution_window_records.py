# meiva:note
# Turning a window's rows into records, over values only.

from __future__ import annotations

from typing import Any, cast

from meiva_builtins.skills.tests.skill_script import load_skill_script

wr = cast(
    Any, load_skill_script("platform", "meiva-self-evolution", "scripts", "window_records.py")
)
record_mod = cast(
    Any, load_skill_script("platform", "meiva-self-evolution", "scripts", "record.py")
)


def test_group_by_agent_keeps_agent_rows_and_drops_service_level_ones() -> None:
    rows = [
        {"agent_id": 2, "event_name": "turn_end", "attributes": {"a": 1}},
        {"agent_id": None, "event_name": "service", "attributes": {}},
        {"agent_id": 1, "event_name": "code", "attributes": None},
        {"agent_id": 2, "event_name": "exec"},
    ]

    grouped = wr.group_by_agent(rows)

    assert grouped == {
        2: [("turn_end", {"a": 1}), ("exec", {})],
        1: [("code", {})],
    }


def test_select_agents_skips_a_missing_lifecycle_row_before_excluding_a_test_spawn() -> None:
    selection = wr.select_agents([1, 2, 3, 4, 5], {1, 2, 3}, {2, 4})

    assert selection == wr.Selection(kept=[1, 3], excluded_test=1, skipped_meta=2)


def test_select_agents_keeps_everyone_when_nothing_is_filtered() -> None:
    assert wr.select_agents([3, 1], {1, 3}, set()) == wr.Selection([3, 1], 0, 0)


def test_help_and_subprocess_counters_ride_on_the_record() -> None:
    run = record_mod.RunInputs(
        agent_id=1,
        week="w",
        events=[
            ("code", {"body": "help(meiva.files)\nhelp(len)\nsubprocess.run(['x'])"}),
            ("turn_end", {}),
        ],
        log_events=[],
        inbounds=[],
        meta=record_mod.AgentMeta("user", "terminated", "done"),
        transcript=[],
        skills_touched=[],
    )

    rec = wr.annotated_record(run)

    assert rec["builtin_help_calls"] == 2
    assert rec["builtin_help_on_meiva"] == 1
    assert rec["subprocess_calls"] == 1


def test_window_records_builds_one_record_per_selected_agent_in_order() -> None:
    meta = record_mod.AgentMeta("user", "terminated", "done")
    rows = wr.WindowRows(
        events={1: [("turn_end", {"duration_seconds": 2})], 2: []},
        logs={},
        inbounds={
            1: [{"source": "user", "content": "task", "is_broadcast": False, "claimed": True}]
        },
        meta={1: meta, 2: meta, 3: meta},
        receipts={2: {"note": 1, "status": 0, "results": 0, "state_changes": 0}},
        transcripts={1: [], 2: [{"type": "human", "content": "from transcript"}]},
        skills={1: ["meiva-goal"], 2: []},
    )

    records = wr.window_records([2, 1], "2026-08-13", rows)

    assert [r["agent_id"] for r in records] == [2, 1]
    assert {r["week"] for r in records} == {"2026-08-13"}
    by_id = {r["agent_id"]: r for r in records}
    assert by_id[1]["task_prompt"] == "task"
    assert by_id[1]["skills_touched"] == ["meiva-goal"]
    assert by_id[2]["task_prompt"] == "from transcript"
    assert by_id[2]["evidence"]["task_updates"]["note"] == 1
