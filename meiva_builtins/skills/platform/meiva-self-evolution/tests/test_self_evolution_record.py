from __future__ import annotations

from typing import Any, cast

import pytest

from meiva_builtins.skills.tests.skill_script import load_skill_script

record_module = load_skill_script("platform", "meiva-self-evolution", "scripts", "record.py")
rubric_module = load_skill_script("platform", "meiva-self-evolution", "scripts", "rubric.py")


@pytest.fixture()
def record() -> Any:
    return cast(Any, record_module)


@pytest.mark.parametrize(
    ("event", "is_ok", "is_fail"),
    [
        ("exec", True, False),
        ("exec_failed", False, True),
        ("exec_timeout", False, True),
        ("exec_node_timeout", False, True),
        ("exec_cancelled", False, True),
        ("exec_subprocess_aborted", False, True),
        ("exec_subprocess_killed", False, True),
        ("exec_result_envelope_invalid", False, True),
        ("exec_envelope", False, False),
        ("exec_start", False, False),
        ("exec_output", False, False),
        ("exec_output_chunk", False, False),
        ("turn_end", False, False),
    ],
)
def test_exec_outcome_classification(record: Any, event: str, is_ok: bool, is_fail: bool) -> None:
    assert record._is_exec_ok(event) is is_ok
    assert record._is_exec_fail(event) is is_fail


def test_count_tool_calls_ignores_syntax_warnings(
    record: Any, recwarn: pytest.WarningsRecorder
) -> None:
    codes = [
        "import meiva\nmeiva.files.write('x', 'y')",
        "re.compile('a\\|b')",
        "meiva.shell.run('ls')",
    ]
    counts = record.count_tool_calls(codes)
    assert counts == {"meiva.files.write": 1, "meiva.shell.run": 1}
    assert not [w for w in recwarn.list if issubclass(w.category, SyntaxWarning)]


def _label_record(*, turns: int, exec_failed: int) -> dict[str, Any]:
    return {
        "tools_called": {},
        "terminated": False,
        "final_output": "delivered",
        "turns": turns,
        "last_exec_failed": False,
        "followup_prompts": [],
        "corrections": [],
        "peer_feedback": [],
        "exec_failed": exec_failed,
        "compactions": 0,
    }


def _inbound(source: str, content: str, *, broadcast: bool = False) -> dict[str, Any]:
    return {"source": source, "content": content, "is_broadcast": broadcast, "claimed": False}


# meiva:note
# The inputs of a record built from the empty case of everything, overridden by `fields`.
def _run(**fields: Any) -> Any:
    values: dict[str, Any] = {
        "agent_id": 7,
        "week": "2026-08-30",
        "events": [],
        "log_events": [],
        "inbounds": [],
        "meta": record_module.AgentMeta("user", "terminated", ""),
        "transcript": [],
        "skills_touched": [],
    }
    values.update(fields)
    return record_module.RunInputs(**values)


def _build(
    record: Any,
    *,
    inbounds: list[dict[str, Any]] | None = None,
    transcript: list[dict[str, str]] | None = None,
    spawner: str = "user",
) -> dict[str, Any]:
    return record.build_record(
        _run(
            inbounds=inbounds or [],
            transcript=transcript if transcript is not None else [],
            meta=record_module.AgentMeta(spawner, "terminated", ""),
        )
    )


@pytest.mark.parametrize(
    ("turns", "exec_failed", "expected"),
    [
        pytest.param(5, 3, "fumbled", id="short-worker-keeps-three-failure-floor"),
        pytest.param(204, 13, "ok", id="agent-4731-iteration-failures-are-not-a-fumble"),
    ],
)
def test_exec_failure_fumble_threshold_scales_with_turns(
    record: Any, turns: int, exec_failed: int, expected: str
) -> None:
    assert record.label(_label_record(turns=turns, exec_failed=exec_failed)) == expected


def test_broadcast_instruction_is_excluded_from_per_agent_corrections(record: Any) -> None:
    rec = record.build_record(
        _run(
            events=[("llm_usage", {"model": "deepseek", "in_total": 900, "out_total": 200})],
            inbounds=[
                _inbound("user", "task prompt"),
                _inbound("user", "wrong: pause all work until resumed", broadcast=True),
            ],
            meta=record_module.AgentMeta("user", "terminated", "delivered"),
        )
    )

    assert rec["corrections"] == []
    assert rec["followup_prompts"] == []
    assert rec["label"] == "ok"


def test_targeted_correction_remains_a_per_agent_correction(record: Any) -> None:
    rec = record.build_record(
        _run(
            events=[("llm_usage", {"model": "deepseek", "in_total": 900, "out_total": 200})],
            inbounds=[
                _inbound("user", "task prompt"),
                _inbound("user", "wrong: use the requested path"),
            ],
            meta=record_module.AgentMeta("user", "terminated", "delivered"),
        )
    )

    assert rec["corrections"] == ["wrong: use the requested path"]
    assert rec["label"] == "fumbled"


@pytest.mark.parametrize(
    ("spawner", "inbounds", "transcript", "expected", "source"),
    [
        pytest.param(
            "agent:1",
            [_inbound("user", "human brief")],
            [],
            "human brief",
            "human",
            id="human-first",
        ),
        pytest.param(
            "agent:1",
            [_inbound("ui:web", "web console brief")],
            [],
            "web console brief",
            "human",
            id="legacy-web-console-is-human",
        ),
        pytest.param(
            "agent:1",
            [_inbound("agent:1", "spawn brief")],
            [],
            "spawn brief",
            "spawner",
            id="spawner-brief",
        ),
        pytest.param(
            "agent:9",
            [_inbound("schedule:7", "scheduled brief")],
            [],
            "scheduled brief",
            "schedule",
            id="schedule-brief",
        ),
        pytest.param(
            "agent:1",
            [],
            [
                {
                    "type": "human",
                    "content": f"{record_module.SYSTEM_NOTE_OPEN}\nYour Agent ID is 7.\n</system_note>",
                },
                {"type": "human", "content": "inherited brief"},
            ],
            "inherited brief",
            "transcript",
            id="transcript-fallback",
        ),
        pytest.param(
            "agent:1",
            [],
            [
                {
                    "type": "human",
                    "content": f"{record_module.SYSTEM_NOTE_OPEN}\nYour Agent ID is 7.\n</system_note>",
                }
            ],
            "",
            "none",
            id="preamble-only",
        ),
    ],
)
def test_task_prompt_origin(
    record: Any,
    spawner: str,
    inbounds: list[dict[str, Any]],
    transcript: list[dict[str, str]],
    expected: str,
    source: str,
) -> None:
    rec = _build(record, inbounds=inbounds, transcript=transcript, spawner=spawner)

    assert rec["task_prompt"] == expected
    assert rec["task_prompt_source"] == source


def test_the_human_message_that_carried_the_task_is_not_a_followup(record: Any) -> None:
    inbounds = [
        _inbound("agent:1", "spawn brief"),
        _inbound("user", "also check the logs"),
        _inbound("user", "and then the metrics"),
    ]

    rec = _build(record, inbounds=inbounds, spawner="agent:1")

    assert rec["task_prompt"] == "also check the logs"
    assert rec["task_prompt_source"] == "human"
    assert rec["followup_prompts"] == ["and then the metrics"]


def test_task_prompt_of_falls_back_to_the_transcript(record: Any) -> None:
    legacy = {
        "task_prompt": "",
        "transcript": [
            {
                "type": "human",
                "content": f"{record_module.SYSTEM_NOTE_OPEN}\nYour Agent ID is 7.\n</system_note>",
            },
            {"type": "human", "content": "Fix the flaky test."},
        ],
    }
    collected = {"task_prompt": "Fix the flaky test.", "transcript": []}

    assert record.task_prompt_of(legacy) == "Fix the flaky test."
    assert record.task_prompt_of(collected) == "Fix the flaky test."


def test_msg_text_skips_thinking_and_tool_use_blocks(record: Any) -> None:
    silent_turn = [
        {"type": "thinking", "thinking": "internal reasoning"},
        {"type": "tool_use", "name": "execute_code", "input": {"code": "import meiva"}},
    ]
    spoken_turn = [
        {"type": "thinking", "thinking": "internal reasoning"},
        {"type": "text", "text": "Checking the logs now."},
        {"type": "tool_use", "name": "execute_code", "input": {"code": "import meiva"}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
    ]

    assert record._msg_text(silent_turn) == ""
    assert record._msg_text(spoken_turn) == "Checking the logs now. image_url"


_EVIDENCE_FIELDS = frozenset(
    {
        "telemetry_events",
        "audit_events",
        "tokens_in",
        "tokens_out",
        "turns",
        "task_prompt_chars",
        "prompt_source",
        "final_output_chars",
        "terminal_status",
        "inbound_total",
        "inbound_claimed",
        "task_updates",
        "task_state_changes",
        "exec_ok",
        "exec_failed",
    }
)


def _usage_events() -> list[tuple[str, dict[str, Any]]]:
    return [
        ("llm_usage", {"model": "deepseek", "in_total": 1200, "out_total": 300}),
        ("turn_end", {"duration_seconds": 4.0}),
    ]


def _user_inbound(content: str = "ship the evidence block") -> dict[str, Any]:
    return {"source": "user", "content": content, "is_broadcast": False, "claimed": True}


def _evidence_record(
    record: Any,
    *,
    events: list[tuple[str, dict[str, Any]]],
    inbounds: list[dict[str, Any]],
    status: str | None,
    task_receipts: dict[str, int] | None = None,
) -> dict[str, Any]:
    return record.build_record(
        _run(
            week="2026-10-17",
            events=events,
            inbounds=inbounds,
            meta=record_module.AgentMeta("user", status, "delivered"),
            task_receipts=task_receipts,
        )
    )


def test_evidence_grade_none_without_telemetry(record: Any) -> None:
    rec = _evidence_record(record, events=[], inbounds=[], status="terminated")

    assert rec["evidence"]["telemetry_events"] == 0
    assert rec["evidence_grade"] == "none"
    assert rec["unknown_reason"] == "no_telemetry"
    assert rec["schema_version"] == 2


def test_evidence_grade_partial_without_terminal_status(record: Any) -> None:
    rec = _evidence_record(
        record,
        events=_usage_events(),
        inbounds=[_user_inbound()],
        status=None,
    )

    assert rec["evidence"]["terminal_status"] is None
    assert rec["evidence_grade"] == "partial"
    assert rec["unknown_reason"] == "no_terminal"


def test_evidence_grade_partial_for_live_status(record: Any) -> None:
    rec = _evidence_record(
        record,
        events=_usage_events(),
        inbounds=[_user_inbound()],
        status="idling",
    )

    assert rec["evidence"]["terminal_status"] == "idling"
    assert rec["evidence_grade"] == "partial"
    assert rec["unknown_reason"] == "no_terminal"


def test_evidence_grade_complete_when_all_evidence_present(record: Any) -> None:
    rec = _evidence_record(
        record,
        events=_usage_events(),
        inbounds=[_user_inbound()],
        status="terminated",
    )

    assert rec["evidence_grade"] == "complete"
    assert rec["unknown_reason"] is None


def test_evidence_grade_partial_without_task_prompt(record: Any) -> None:
    rec = _evidence_record(
        record,
        events=_usage_events(),
        inbounds=[],
        status="terminated",
    )

    assert rec["evidence"]["task_prompt_chars"] == 0
    assert rec["evidence"]["prompt_source"] == "none"
    assert rec["evidence_grade"] == "partial"
    assert rec["unknown_reason"] == "no_task"


def test_legacy_record_without_evidence_keeps_downstream_reads(record: Any) -> None:
    rec = _evidence_record(
        record,
        events=_usage_events(),
        inbounds=[_user_inbound()],
        status="terminated",
    )
    legacy = {
        key: value
        for key, value in rec.items()
        if key not in {"evidence", "evidence_grade", "unknown_reason", "schema_version"}
    }

    assert legacy.get("evidence") is None
    assert legacy.get("evidence_grade") is None
    assert legacy.get("unknown_reason") is None
    assert record.label(legacy) == rec["label"]
    assert rubric_module.scores(legacy) == rubric_module.scores(rec)


def test_evidence_block_reports_prompt_source_and_task_receipts(record: Any) -> None:
    rec = record.build_record(
        _run(
            week="2026-10-17",
            inbounds=[_inbound("agent:12", "peer brief")],
            meta=record_module.AgentMeta("agent:12", "idling", "delivered"),
            task_receipts={"note": 3, "status": 1, "results": 2, "state_changes": 1},
        )
    )

    evidence = rec["evidence"]
    assert evidence["prompt_source"] == "spawner"
    assert evidence["inbound_total"] == 1
    assert evidence["inbound_claimed"] == 0
    assert evidence["task_updates"] == {"note": 3, "status": 1, "results": 2}
    assert evidence["task_state_changes"] == 1
    assert set(evidence) == _EVIDENCE_FIELDS

    no_receipts = record.build_record(
        _run(week="2026-10-17", meta=record_module.AgentMeta("user", "terminated", "delivered"))
    )["evidence"]
    assert no_receipts["task_updates"] == {"note": 0, "status": 0, "results": 0}
    assert no_receipts["task_state_changes"] == 0
