import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import psutil
import pytest
from loguru import logger

from agent.graph.exec._owned_run import OwnedRun, validate_closed
from base.agents.incarnation.exec_owner_protocol import OwnerClosed, OwnerContext
from base.agents.incarnation.resources import (
    ExecAllocation,
    ResourceEvidenceError,
    ResourceProcess,
    exec_identity_digest,
)


def _run(**fields: Any) -> OwnedRun:
    run = object.__new__(OwnedRun)
    defaults: dict[str, Any] = {
        "request_id": uuid4(),
        "request": "req",
        "scope": None,
        "proc": MagicMock(),
        "ready": MagicMock(),
        "reader": MagicMock(),
        "settled": False,
        "attached": True,
        "registration": None,
        "completion": None,
        "bound": time.monotonic() + 5,
        "_tasks": set(),
        "_errors": [],
    }
    for name, value in {**defaults, **fields}.items():
        setattr(run, name, value)
    return run


async def test_unsettled_attached_owner_is_retained_by_the_hosted_scope() -> None:
    run = _run(scope=SimpleNamespace(completions=set()))
    receipt = MagicMock()
    run.settle_attached_owner = AsyncMock(return_value=receipt)

    async def completion() -> Any:
        return await run.settle_attached_owner()

    task = asyncio.ensure_future(completion())
    run.attached_completion = lambda: task

    assert run.needs_hand_off() is True
    await run.finish_owner()
    run.settle_attached_owner.assert_awaited_once()


async def test_owner_that_cannot_settle_raises_its_original_failure() -> None:
    run = _run(scope=SimpleNamespace(completions=set()))
    failure = RuntimeError("owner still open")

    async def failing() -> Any:
        raise failure

    task = asyncio.ensure_future(failing())
    run.attached_completion = lambda: task

    with pytest.raises(RuntimeError) as observed:
        await run.finish_owner()
    assert observed.value is failure


async def test_settled_unhosted_or_unattached_owner_is_not_handed_off() -> None:
    scope = SimpleNamespace(completions=set())
    assert _run(scope=scope, settled=True).needs_hand_off() is False
    assert _run(scope=None).needs_hand_off() is False
    assert _run(scope=scope, attached=False).needs_hand_off() is False


async def test_cancelled_registration_that_failed_settles_the_unpermitted_owner() -> None:
    from base.agents.incarnation.resources import ResourceEvidenceError

    scope = MagicMock()
    run = _run(scope=scope, attached=False)

    async def failed() -> None:
        raise ResourceEvidenceError("registration refused")

    registration = asyncio.ensure_future(failed())
    run.settle_unpermitted_owner = AsyncMock()
    original = asyncio.CancelledError()

    await run.settle_registration_after_cancel(registration, original)

    assert run.settled is True and run.attached is False
    scope.complete.assert_called_once_with(run.request, None)
    assert not getattr(original, "__notes__", [])


async def test_unresolved_unpermitted_owner_leaves_a_note_and_stays_unsettled() -> None:
    from base.agents.incarnation.resources import ResourceEvidenceError

    scope = MagicMock()
    run = _run(scope=scope, attached=False)

    async def failed() -> None:
        raise ResourceEvidenceError("registration refused")

    registration = asyncio.ensure_future(failed())
    run.settle_unpermitted_owner = AsyncMock(side_effect=RuntimeError("still open"))
    original = asyncio.CancelledError()

    await run.settle_registration_after_cancel(registration, original)

    assert run.settled is False
    scope.complete.assert_not_called()
    assert any("still open" in note for note in original.__notes__)


_AGENT = 9


def _process(pid: int) -> ResourceProcess:
    return ResourceProcess.capture(psutil.Process()).model_copy(update={"pid": pid})


def _domain(
    tmp_path: Path,
    allocation: ExecAllocation,
    *,
    payload: bytes,
    generation: UUID,
    owner: UUID,
) -> tuple[OwnerContext, ExecAllocation, Path]:
    attached = allocation.model_copy(
        update={"owner_process": _process(41), "root_process": _process(42)}
    )
    root = tmp_path / "exec" / str(_AGENT) / "domains" / str(allocation.request)
    root.mkdir(parents=True)
    request = (root / f"req-{allocation.request.hex}.json").resolve()
    request.write_bytes(payload)
    context = OwnerContext(
        agent_id=_AGENT,
        generation=generation,
        runtime_owner=owner,
        request_path=request,
        result_path=(root / "result.json").resolve(),
        allocation=allocation,
    )
    closed = root / "owner.closed"
    closed.write_text(
        OwnerClosed(
            allocation=attached,
            root_exit_code=0,
            reason="completed",
            observed_at=datetime.now(UTC),
        ).model_dump_json()
    )
    return context, attached, closed


def _version_1(payload: bytes) -> ExecAllocation:
    return ExecAllocation(
        request=uuid4(),
        domain=uuid4(),
        request_digest=hashlib.sha256(payload).hexdigest(),
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    )


def _version_2(payload: bytes, *, generation: UUID, owner: UUID) -> ExecAllocation:
    request, domain = uuid4(), uuid4()
    deadline = datetime.now(UTC) + timedelta(seconds=30)
    return ExecAllocation(
        request=request,
        domain=domain,
        request_digest=exec_identity_digest(
            request=request,
            domain=domain,
            agent_id=_AGENT,
            generation=generation,
            runtime_owner=owner,
            deadline=deadline,
        ),
        deadline=deadline,
        digest_version=2,
        request_sha256=hashlib.sha256(payload).hexdigest(),
    )


def test_a_legacy_allocation_with_identical_bytes_settles(tmp_path: Path) -> None:
    payload = b'{"v": 1, "code": "print(1)"}'
    allocation = _version_1(payload)
    context, attached, closed = _domain(
        tmp_path, allocation, payload=payload, generation=uuid4(), owner=uuid4()
    )

    assert validate_closed(context, attached, closed).reason == "completed"


def test_a_legacy_allocation_with_rewritten_bytes_still_refuses(tmp_path: Path) -> None:
    allocation = _version_1(b"first")
    context, attached, closed = _domain(
        tmp_path, allocation, payload=b"second", generation=uuid4(), owner=uuid4()
    )

    with pytest.raises(ResourceEvidenceError, match="terminal owner request has changed"):
        validate_closed(context, attached, closed)


def test_a_version_2_allocation_settles_a_rewritten_envelope_and_reports_it(
    tmp_path: Path,
) -> None:
    generation, owner = uuid4(), uuid4()
    allocation = _version_2(b'{"v": 1, "code": "print(1)"}', generation=generation, owner=owner)
    context, attached, closed = _domain(
        tmp_path,
        allocation,
        payload=b'{"v": 1, "code": "scrub()"}',
        generation=generation,
        owner=owner,
    )
    records: list[Any] = []
    sink = logger.add(
        lambda message: records.append(message.record), level="ERROR", format="{message}"
    )
    try:
        receipt = validate_closed(context, attached, closed)
    finally:
        logger.remove(sink)

    assert receipt.reason == "completed"
    assert [record["extra"]["event"] for record in records] == ["exec_request_envelope_mutated"]
    assert records[0]["extra"]["refused"] is False
    evidence = tmp_path / "exec" / str(_AGENT) / "evidence" / f"{allocation.request}.json"
    assert json.loads(evidence.read_text())["refused"] is False


def test_an_identity_that_no_longer_derives_refuses(tmp_path: Path) -> None:
    payload = b'{"v": 1}'
    allocation = _version_2(payload, generation=uuid4(), owner=uuid4())
    context, attached, closed = _domain(
        tmp_path, allocation, payload=payload, generation=uuid4(), owner=uuid4()
    )

    with pytest.raises(ResourceEvidenceError, match="identity differs"):
        validate_closed(context, attached, closed)


def test_a_version_2_allocation_settles_when_the_envelope_is_gone(tmp_path: Path) -> None:
    payload = b'{"v": 1, "code": "print(1)"}'
    generation, owner = uuid4(), uuid4()
    allocation = _version_2(payload, generation=generation, owner=owner)
    context, attached, closed = _domain(
        tmp_path, allocation, payload=payload, generation=generation, owner=owner
    )
    context.request_path.unlink()

    assert validate_closed(context, attached, closed).reason == "completed"
