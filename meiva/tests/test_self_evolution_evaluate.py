from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import psycopg
import pytest

from base.cluster.machine import MachineIdentityReader
from base.config.tests.config_authority import config_authority as config_authority
from base.db import Database
from base.packages.tests.model_catalog import model_catalog as model_catalog
from meiva.gateway_client import use_client
from meiva.tests.fixtures.model_owner import model_installation as model_installation
from meiva.tests.fixtures.model_owner import sdk_model_owner as sdk_model_owner
from meiva.tests.fixtures.pin_agent import pin_agent


def _evaluate_module() -> ModuleType:
    path = (
        Path(__file__).parents[2]
        / "meiva_builtins/skills/platform/meiva-self-evolution/scripts/evaluate.py"
    )
    previous_path = sys.path.copy()
    try:
        sys.path.insert(0, str(path.parent))
        spec = importlib.util.spec_from_file_location("test_evaluate", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = previous_path
    return module


@pytest.mark.parametrize(
    ("model", "expected_overlay"),
    [
        (None, {"eval_isolation": True}),
        ("claude-sonnet-5", {"eval_isolation": True, "llm_model": "claude-sonnet-5"}),
    ],
)
@pytest.mark.usefixtures("sdk_model_owner")
def test_launch_spawns_eval_isolated_agents(
    machine_identity: MachineIdentityReader,
    model: str | None,
    expected_overlay: dict[str, object],
) -> None:
    evaluate = _evaluate_module()
    pin_agent(7, machine=machine_identity)
    received: list[dict[str, Any]] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        received.append(json.loads(request.content))
        return httpx.Response(201, json={"id": 42}, request=request)

    with (
        httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(gateway)) as http,
        use_client(http),
    ):
        state = evaluate.launch(
            "meiva-goal",
            [{"agent_id": 9, "task_prompt": "Inspect the output", "tools_called": []}],
            model=model,
        )
    Path(state["path"]).unlink()

    assert [body["config"] for body in received] == [expected_overlay]
    assert [body["prompt"] for body in received] == ["Inspect the output"]
    assert state["runs"][0]["eval_agent_id"] == 42
    assert state["runs"][0]["original_tools_called"] == []


@pytest.mark.parametrize(
    ("original", "replayed", "expected"),
    [
        (
            {"tools_called": {"meiva.files.read": 1}},
            {"turns": 1, "final_output": "done", "tools_called": {}},
            False,
        ),
        (
            {"tools_called": {"meiva.files.read": 1}},
            {
                "turns": 1,
                "final_output": "done",
                "tools_called": {"meiva.files.read": 1, "meiva.web.search": 1},
            },
            True,
        ),
        (
            {"tools_called": {"meiva.files.read": 1}},
            {"turns": 1, "final_output": "done", "tools_called": {"meiva.shell.run": 1}},
            False,
        ),
        (
            {"tools_called": {}},
            {"turns": 1, "final_output": "done", "tools_called": {}},
            True,
        ),
        (
            {"tools_called": {}},
            {"turns": 0, "final_output": "done", "tools_called": {}},
            False,
        ),
        (
            {"tools_called": {}},
            {"turns": 1, "final_output": "   ", "tools_called": {}},
            False,
        ),
    ],
)
def test_verify_replay(
    original: dict[str, object], replayed: dict[str, object], expected: bool
) -> None:
    evaluate = _evaluate_module()

    ok, _reason = evaluate.verify_replay(original, replayed)

    assert ok is expected


def _record(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "label": "ok",
        "task_prompt": "Inspect the output",
        "turns": 1,
        "final_output": "done",
        "tools_called": {},
        "exec_ok": 1,
        "exec_failed": 0,
        "last_exec_failed": False,
        "compactions": 0,
        "followup_prompts": [],
        "tokens_in": 1000,
        "tokens_out": 500,
        "leak_audit": [],
        "invalidated": False,
    }
    record.update(overrides)
    return record


def _seed_statuses(db_conn: psycopg.Connection[Any], statuses: dict[int, str]) -> None:
    for agent_id, status in statuses.items():
        db_conn.execute(
            "INSERT INTO agents (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (agent_id,)
        )
        db_conn.execute(
            "INSERT INTO agents_meta (id, spawner, status, session_index) "
            "VALUES (%s, 'test', %s, 0)",
            (agent_id, status),
        )
    db_conn.commit()


def test_split_progress_separates_finished_agents_from_running_ones() -> None:
    evaluate = _evaluate_module()

    progress = evaluate.split_progress(
        [41, 42, 44, 45, 46],
        {41: "idling", 42: "terminated", 44: "idling", 45: "running"},
    )

    assert progress == {"done": [41, 42, 44], "pending": [45, 46]}


def test_poll_reads_the_statuses_of_the_eval_agents(
    database: Database, db_conn: psycopg.Connection[Any]
) -> None:
    evaluate = _evaluate_module()
    _seed_statuses(db_conn, {51: "idling", 52: "running"})
    state = {"runs": [{"eval_agent_id": 51}, {"eval_agent_id": 52}, {"eval_agent_id": 53}]}

    assert evaluate.poll(state, database=database) == {"done": [51], "pending": [52, 53]}


def test_the_report_excludes_invalid_replays_from_the_mean() -> None:
    evaluate = _evaluate_module()
    state: dict[str, Any] = {
        "skill": "meiva-goal",
        "stamp": "2026-08-23T00-00-00Z",
        "runs": [
            {"eval_agent_id": 41, "source_agent_id": 1, "original_tools_called": {}},
            {
                "eval_agent_id": 42,
                "source_agent_id": 2,
                "original_tools_called": {"meiva.files.read": 1},
            },
            {"eval_agent_id": 44, "source_agent_id": 3, "original_tools_called": {}},
            {"eval_agent_id": 45, "source_agent_id": 4, "original_tools_called": {}},
        ],
        "skipped": [],
    }
    records = {
        41: _record(),
        42: _record(),
        44: _record(turns=0, exec_ok=0, final_output="done"),
    }

    report = evaluate.assemble_report(state, [45], records)

    perfect = evaluate.scores(records[41])
    assert report["n"] == 1
    assert report["mean"] == {key: perfect[key] for key in ("completion", "efficiency", "overall")}
    assert report["pending"] == [45]
    assert [entry["valid"] for entry in report["per_task"]] == [True, False, False]
    assert [entry["eval_agent_id"] for entry in report["invalid"]] == [42, 44]
    assert report["invalid"][0]["verification"]["reason"].startswith("replay tool profile diverges")
    assert report["invalid"][1]["verification"]["reason"] == "abstain:no_telemetry"

    empty = evaluate.assemble_report({**state, "runs": [state["runs"][2]]}, [], {44: records[44]})
    assert empty["n"] == 0
    assert empty["mean"] is None
    assert evaluate._mean([]) is None


def test_the_report_excludes_leaked_replays_from_the_mean() -> None:
    evaluate = _evaluate_module()
    state: dict[str, Any] = {
        "skill": "meiva-goal",
        "stamp": "test",
        "runs": [
            {"eval_agent_id": 42, "source_agent_id": 1, "original_tools_called": {}},
            {"eval_agent_id": 43, "source_agent_id": 2, "original_tools_called": {}},
        ],
        "skipped": [],
    }
    leak = [{"surface": "results", "evidence": "read", "tool": None}]
    records = {42: _record(), 43: _record(leak_audit=leak, invalidated=True)}

    report = evaluate.assemble_report(state, [], records)

    clean = evaluate.scores(records[42])
    assert report["n"] == 1
    assert report["mean"]["overall"] == clean["overall"]
    assert report["invalidated"] == 1
    assert report["per_task"][1]["invalidated"] is True
    assert report["per_task"][1]["leak_audit"] == leak
    assert report["invalid"][0]["verification"]["reason"] == "leak"


def test_the_report_does_not_change_the_records_it_is_given() -> None:
    evaluate = _evaluate_module()
    state: dict[str, Any] = {
        "skill": "meiva-goal",
        "stamp": "test",
        "runs": [{"eval_agent_id": 42, "source_agent_id": 1, "original_tools_called": {}}],
        "skipped": [],
    }
    record = _record(task_prompt="")
    before = dict(record)

    evaluate.assemble_report(state, [], {42: record})

    assert record == before
