import hashlib
import importlib.util
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from meiva.tests.fixtures.gateway_wire import GatewayStub, GatewayWire, Reply
from meiva.tests.fixtures.gateway_wire import gateway_stub as gateway_stub
from meiva.tests.fixtures.pin_agent import pin_agent

REFERENCE = Path(__file__).resolve().parents[1] / "references/minimal_dispatch.py"
TASKS = [{"id": "one", "prompt": "Analyze input one"}, {"id": "two", "prompt": "Analyze input two"}]
_SPAWN = "/api/keyed/v1/agents"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("minimal_dispatch_example", REFERENCE)
    assert spec is not None and spec.loader is not None
    module: Any = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dispatch = _load()


@pytest.fixture
def gateway(sdk_environment: object, gateway_stub: GatewayStub) -> Iterator[GatewayWire]:
    """The gateway as the dispatching agent 7 sees it: a listener on port 0 that numbers the
    peers it accepts 101, 102, ... and a client that makes its bounded attempts without waiting."""
    with GatewayWire.open(gateway_stub, retries=2) as wire:
        pin_agent(7, clients=wire.context.clients)
        wire.stub.reply_in_turn(
            "POST",
            _SPAWN,
            [Reply(201, {"id": 101}), Reply(201, {"id": 102}), Reply(201, {"id": 103})],
        )
        yield wire


def _spawned(wire: GatewayWire) -> list[str]:
    return [sent.body["prompt"] for sent in wire.sent if sent.path == _SPAWN]


def write_result(root: Path, task: dict[str, str], **overrides: Any) -> None:
    value = {
        "task_id": task["id"],
        "input_hash": hashlib.sha256(task["prompt"].encode()).hexdigest(),
        "result": {"evidence": "verified"},
    }
    value.update(overrides)
    (root / f"{task['id']}.json").write_text(json.dumps(value))


def test_reentry_preserves_known_peers_and_completed_results(
    gateway: GatewayWire, tmp_path: Path
) -> None:
    first = dispatch.run(tmp_path, TASKS)
    assert first["pending"] == {"one": 101, "two": 102}
    assert dispatch.run(tmp_path, TASKS) == first
    assert len(_spawned(gateway)) == 2
    for task in TASKS:
        write_result(tmp_path, task)
    before = (tmp_path / "one.json").read_bytes()
    done = dispatch.run(tmp_path, TASKS)
    assert set(done["results"]) == {"one", "two"}
    assert done["pending"] == {}
    assert len(_spawned(gateway)) == 2
    assert (tmp_path / "one.json").read_bytes() == before


def test_each_peer_is_told_where_and_how_to_deliver_under_its_own_key(
    gateway: GatewayWire, tmp_path: Path
) -> None:
    dispatch.run(tmp_path, TASKS)

    sent = [s for s in gateway.sent if s.path == _SPAWN]
    assert [s.body["prompt"].split("\n")[0] for s in sent] == [t["prompt"] for t in TASKS]
    assert len({s.headers["idempotency-key"] for s in sent}) == 2
    assert f"Write your result atomically to {tmp_path / 'one.json'}" in sent[0].body["prompt"]
    state = json.loads((tmp_path / "dispatch.json").read_text())
    assert [state[t["id"]]["phase"] for t in TASKS] == ["assigned", "assigned"]


def test_spawn_failure_persists_ambiguity_and_stops_batch(
    gateway: GatewayWire, tmp_path: Path
) -> None:
    gateway.stub.reply_in_turn("POST", _SPAWN, [Reply(201, {"id": 101}), Reply(503, {})])

    with pytest.raises(httpx.HTTPStatusError):
        dispatch.run(tmp_path, TASKS)
    state = json.loads((tmp_path / "dispatch.json").read_text())
    assert state["one"]["agent_id"] == 101
    assert state["two"]["phase"] == "dispatching"
    reached = len(gateway.sent)
    with pytest.raises(RuntimeError, match="ambiguous dispatch"):
        dispatch.run(tmp_path, TASKS)
    assert len(gateway.sent) == reached


# meiva:note
# The receipt cannot be committed atomically with the spawn. Here the disk fails between them: while the gateway is still answering the first spawn, a directory takes the name of the temporary file the receipt is written through.
def test_crash_after_acceptance_does_not_spawn_again(gateway: GatewayWire, tmp_path: Path) -> None:
    release = threading.Event()
    gateway.stub.hold("POST", _SPAWN, release, {"id": 101}, status=201)

    def break_the_receipt_write() -> None:
        gateway.stub.arrived.wait(10)
        (tmp_path / "dispatch.tmp").mkdir()
        release.set()

    saboteur = threading.Thread(target=break_the_receipt_write)
    saboteur.start()
    try:
        with pytest.raises(OSError):
            dispatch.run(tmp_path, TASKS)
    finally:
        release.set()
        saboteur.join()
    with pytest.raises(RuntimeError, match="ambiguous dispatch"):
        dispatch.run(tmp_path, TASKS)
    assert len(_spawned(gateway)) == 1


@pytest.mark.parametrize(
    "overrides", [{"input_hash": "old-input"}, {"task_id": "other"}, {"result": "partial"}]
)
def test_invalid_result_stops_before_any_spawn(
    gateway: GatewayWire, tmp_path: Path, overrides: dict[str, Any]
) -> None:
    write_result(tmp_path, TASKS[1], **overrides)
    with pytest.raises(ValueError, match="invalid or stale"):
        dispatch.run(tmp_path, TASKS)
    assert gateway.sent == []


def test_changed_task_cannot_adopt_old_receipt(gateway: GatewayWire, tmp_path: Path) -> None:
    dispatch.run(tmp_path, TASKS)
    changed = [{"id": "one", "prompt": "Different source input"}, TASKS[1]]
    with pytest.raises(ValueError, match="changed"):
        dispatch.run(tmp_path, changed)
    assert len(_spawned(gateway)) == 2


def test_unknown_phase_fails_before_dispatch(gateway: GatewayWire, tmp_path: Path) -> None:
    state = {
        "one": {
            "input_hash": hashlib.sha256(TASKS[0]["prompt"].encode()).hexdigest(),
            "phase": "typo",
        }
    }
    (tmp_path / "dispatch.json").write_text(json.dumps(state))
    with pytest.raises(ValueError, match="unknown dispatch phase"):
        dispatch.run(tmp_path, TASKS)
    assert gateway.sent == []


def test_a_malformed_batch_is_refused_before_anything_is_read_or_created(
    gateway: GatewayWire, tmp_path: Path
) -> None:
    handoff = tmp_path / "run"
    for tasks in (
        [],
        [{"id": "../escape", "prompt": "x"}],
        [{"id": "dup", "prompt": "x"}, {"id": "dup", "prompt": "y"}],
        [{"id": "blank", "prompt": "  "}],
    ):
        with pytest.raises(ValueError):
            dispatch.run(handoff, tasks)
    assert not handoff.exists()
    assert gateway.sent == []


def _plan(state: dict[str, Any], texts: dict[str, str | None]) -> Any:
    return dispatch.plan_dispatch(Path("run"), TASKS, state, texts)


def _receipt(task: dict[str, str], phase: str, **fields: Any) -> dict[str, Any]:
    return {"input_hash": dispatch.fingerprint(task["prompt"]), "phase": phase, **fields}


def _envelope(task: dict[str, str], result: Any = None) -> str:
    return json.dumps(
        {
            "task_id": task["id"],
            "input_hash": dispatch.fingerprint(task["prompt"]),
            "result": {"evidence": "ok"} if result is None else result,
        }
    )


def test_a_fresh_batch_prepares_every_task_in_order() -> None:
    plan = _plan({}, {"one": None, "two": None})

    assert [(p.task_id, p.path) for p in plan.prepared] == [
        ("one", Path("run/one.json")),
        ("two", Path("run/two.json")),
    ]
    assert (plan.results, plan.pending) == ({}, {})


def test_a_known_peer_stays_pending_and_a_finished_task_keeps_its_result() -> None:
    state = {
        "one": _receipt(TASKS[0], "assigned", agent_id=101),
        "two": _receipt(TASKS[1], "assigned", agent_id=102),
    }

    plan = _plan(state, {"one": None, "two": _envelope(TASKS[1])})

    assert plan.pending == {"one": 101}
    assert plan.results == {"two": {"evidence": "ok"}}
    assert plan.prepared == []


def test_a_result_wins_over_an_unreconciled_intent() -> None:
    state = {
        "one": _receipt(TASKS[0], "dispatching"),
        "two": _receipt(TASKS[1], "assigned", agent_id=5),
    }

    plan = _plan(state, {"one": _envelope(TASKS[0]), "two": None})

    assert plan.results == {"one": {"evidence": "ok"}}
    assert plan.pending == {"two": 5}


def test_an_intent_without_a_receipt_or_result_is_ambiguous() -> None:
    with pytest.raises(RuntimeError, match="ambiguous dispatch for one"):
        _plan({"one": _receipt(TASKS[0], "dispatching")}, {"one": None, "two": None})


def test_the_receipt_check_precedes_the_result_check_of_the_same_task() -> None:
    changed = {"one": {"input_hash": "old", "phase": "assigned", "agent_id": 1}}

    with pytest.raises(ValueError, match="task one changed"):
        _plan(changed, {"one": "not json at all", "two": None})


@pytest.mark.parametrize(
    "text",
    [
        json.dumps({"task_id": "one", "input_hash": "stale", "result": {}}),
        json.dumps({"task_id": "two", "input_hash": "x", "result": {}}),
        json.dumps({"task_id": "one", "result": "partial"}),
        json.dumps(["not", "an", "object"]),
    ],
)
def test_a_result_that_is_not_this_tasks_current_envelope_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match=r"invalid or stale result: run/one\.json"):
        _plan({}, {"one": text, "two": None})


def test_unparseable_result_text_is_an_error_not_an_absent_result() -> None:
    with pytest.raises(json.JSONDecodeError):
        _plan({}, {"one": "{", "two": None})
