# meiva:note
# The two routes that answer a workspace file for the machine that owns it: what the gateway cannot
# read from its own disk, it asks for here, and the answer has to be the same document the local
# path builds -- same classes for a refusal, same shape for a file.
# meiva:link base/agents/workspace_files/docs/workspace-files.meiva.okf.md

from __future__ import annotations

import json
from pathlib import Path

from base.db import create_agent
from base.paths import workspace_path
from services.agent_runner.agent_ops.tests.ops_support import Ops
from services.agent_runner.agent_ops.workspace_file import (
    WorkspaceFileRequest,
    content_response,
    raw_response,
)

_LIMIT = 1024 * 1024


def _agent_on(ops: Ops, machine: str) -> int:
    with ops.database().connect() as conn:
        agent = create_agent(conn)
        conn.execute(
            "INSERT INTO agents_meta (id, machine, status) VALUES (%s, %s, 'idling')",
            (agent, machine),
        )
        conn.commit()
    return agent


def _write(ops: Ops, agent: int, name: str, body: bytes) -> Path:
    target = Path(workspace_path(ops.home, agent)) / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return target


def _body(agent: int, path: str, *, max_bytes: int = _LIMIT) -> bytes:
    return (
        WorkspaceFileRequest(agent_id=agent, path=path, max_bytes=max_bytes)
        .model_dump_json()
        .encode()
    )


def test_a_text_file_is_answered_in_the_gateways_own_shape(ops: Ops) -> None:
    agent = _agent_on(ops, ops.machine.name())
    _write(ops, agent, "notes/run.md", b"line one\nline two")

    status, body, content_type = content_response(
        _body(agent, "notes/run.md"),
        pool=ops.pool,
        machine=ops.machine.name(),
        home=ops.home,
        max_bytes=_LIMIT,
    )

    assert (status, content_type) == (200, "application/json")
    document = json.loads(body)
    assert document["content"] == "line one\nline two"
    assert document["path"] == "notes/run.md"
    assert document["binary"] is False


def test_a_path_outside_the_roots_is_refused_with_403(ops: Ops) -> None:
    agent = _agent_on(ops, ops.machine.name())

    status, body, _ = content_response(
        _body(agent, "../../etc/passwd"),
        pool=ops.pool,
        machine=ops.machine.name(),
        home=ops.home,
        max_bytes=_LIMIT,
    )

    assert status == 403
    assert b"outside the agent workspace" in body


def test_a_missing_file_is_404_and_an_over_budget_one_is_413(ops: Ops) -> None:
    agent = _agent_on(ops, ops.machine.name())
    _write(ops, agent, "big.txt", b"x" * 32)

    missing = content_response(
        _body(agent, "absent.txt"),
        pool=ops.pool,
        machine=ops.machine.name(),
        home=ops.home,
        max_bytes=_LIMIT,
    )
    too_large = content_response(
        _body(agent, "big.txt", max_bytes=16),
        pool=ops.pool,
        machine=ops.machine.name(),
        home=ops.home,
        max_bytes=_LIMIT,
    )

    assert missing[0] == 404
    assert too_large[0] == 413


# meiva:note
# The check that keeps a routing mistake from serving a same-named file off the wrong disk: an agent
# whose row names another machine is not answered here at all.
def test_an_agent_of_another_machine_is_not_answered_here(ops: Ops) -> None:
    agent = _agent_on(ops, "somewhere-else")
    _write(ops, agent, "same-name.txt", b"this machine's copy")

    status, _, _ = content_response(
        _body(agent, "same-name.txt"),
        pool=ops.pool,
        machine=ops.machine.name(),
        home=ops.home,
        max_bytes=_LIMIT,
    )

    assert status == 404


def test_the_raw_route_returns_bytes_without_decoding(ops: Ops) -> None:
    agent = _agent_on(ops, ops.machine.name())
    _write(ops, agent, "media/clip.bin", bytes(range(256)))

    status, body, content_type = raw_response(
        _body(agent, "media/clip.bin"),
        pool=ops.pool,
        machine=ops.machine.name(),
        home=ops.home,
        max_bytes=_LIMIT,
    )

    assert (status, content_type) == (200, "application/octet-stream")
    assert body == bytes(range(256))
