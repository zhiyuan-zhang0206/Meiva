from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import psutil
import pytest
from loguru import logger
from psycopg.pq import TransactionStatus
from pydantic import ValidationError

from base.agents.incarnation.exec_owner_protocol import (
    MAX_PAYLOAD_BYTES,
    OwnerClosed,
    OwnerContext,
)
from base.agents.incarnation.exec_owner_recovery import recover_local_resources
from base.agents.incarnation.exec_payload_evidence import observe_exec_payload
from base.agents.incarnation.resources import (
    ExecAllocation,
    IncarnationResources,
    ResourceEvidenceError,
    ResourceProcess,
    exec_identity_digest,
)
from base.db import Database
from base.paths import exec_run_dir


# meiva:note
# A receipt stamped with another boot makes `process_ended` prove the exit from the
# receipt itself, so the recovery path runs without patching the module under test.
def _process(pid: int) -> ResourceProcess:
    return ResourceProcess.capture(psutil.Process()).model_copy(
        update={"pid": pid, "boot_id": str(uuid4())}
    )


def _identity(
    *, request: UUID, domain: UUID, agent_id: int, generation: UUID, owner: UUID, deadline: datetime
) -> str:
    return exec_identity_digest(
        request=request,
        domain=domain,
        agent_id=agent_id,
        generation=generation,
        runtime_owner=owner,
        deadline=deadline,
    )


def _version_2(
    *, payload: bytes, agent_id: int = 7, generation: UUID | None = None, owner: UUID | None = None
) -> tuple[ExecAllocation, int, UUID, UUID]:
    import hashlib

    generation = generation or uuid4()
    owner = owner or uuid4()
    request, domain = uuid4(), uuid4()
    deadline = datetime.now(UTC) + timedelta(seconds=30)
    allocation = ExecAllocation(
        request=request,
        domain=domain,
        request_digest=_identity(
            request=request,
            domain=domain,
            agent_id=agent_id,
            generation=generation,
            owner=owner,
            deadline=deadline,
        ),
        deadline=deadline,
        digest_version=2,
        request_sha256=hashlib.sha256(payload).hexdigest(),
    )
    return allocation, agent_id, generation, owner


def _version_1(*, payload: bytes) -> ExecAllocation:
    import hashlib

    return ExecAllocation(
        request=uuid4(),
        domain=uuid4(),
        request_digest=hashlib.sha256(payload).hexdigest(),
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    )


def _domain_request(root: Path, allocation: ExecAllocation, payload: bytes) -> Path:
    directory = root / str(9) / "domains" / str(allocation.request)
    directory.mkdir(parents=True)
    request = directory / f"req-{allocation.request.hex}.json"
    request.write_bytes(payload)
    return request


def test_identity_digest_depends_only_on_the_allocation_fields() -> None:
    request, domain, generation, owner = uuid4(), uuid4(), uuid4(), uuid4()
    deadline = datetime(2026, 10, 19, 4, 58, 6, 146566, tzinfo=UTC)

    def digest(**changes: Any) -> str:
        return exec_identity_digest(
            request=changes.get("request", request),
            domain=changes.get("domain", domain),
            agent_id=changes.get("agent_id", 6090),
            generation=changes.get("generation", generation),
            runtime_owner=changes.get("runtime_owner", owner),
            deadline=changes.get("deadline", deadline),
        )

    baseline = digest()
    assert baseline == digest()
    assert digest(request=uuid4()) != baseline
    assert digest(domain=uuid4()) != baseline
    assert digest(agent_id=6091) != baseline
    assert digest(generation=uuid4()) != baseline
    assert digest(runtime_owner=uuid4()) != baseline
    assert digest(deadline=deadline + timedelta(microseconds=1)) != baseline
    with pytest.raises(ResourceEvidenceError, match="timezone-aware"):
        digest(deadline=deadline.replace(tzinfo=None))


# meiva:note
# This vector is frozen on purpose: every stored version 2 allocation re-derives its
# identity during recovery, so reordering the canonical lines, printing the deadline
# with `str()` or changing how the agent id is encoded would refuse all of them.
# Changing the canonical form needs a new `digest_version`, not a new value here.
def test_the_identity_digest_canonical_form_is_frozen() -> None:
    digest = exec_identity_digest(
        request=UUID("72643f35-9ed4-4da3-91ac-5c92fdb960c9"),
        domain=UUID("003224e7-4109-4a03-962e-68cbfda89655"),
        agent_id=6090,
        generation=UUID("b9b073aa-7e4f-4b2f-a4a9-1b21a1564168"),
        runtime_owner=UUID("8c99ab4a-7dc7-4fc7-b11c-1112f8fb451e"),
        deadline=datetime(2026, 10, 19, 4, 58, 6, 146566, tzinfo=UTC),
    )

    assert digest == "a88f219f1918307ee979288f47e20887bc55d37e96b3f0cfa006f35b08b47c60"


def test_a_legacy_row_decodes_as_payload_authoritative() -> None:
    legacy = {
        "request": str(uuid4()),
        "domain": str(uuid4()),
        "request_digest": "a" * 64,
        "deadline": "2026-10-19T04:58:06.146566Z",
    }
    allocation = ExecAllocation.model_validate_json(json.dumps(legacy))
    assert allocation.digest_version == 1
    assert allocation.payload_is_authoritative
    assert allocation.request_sha256 is None
    assert allocation.recorded_payload_sha256 == "a" * 64


def test_an_identity_allocation_requires_its_payload_digest() -> None:
    with pytest.raises(ValidationError, match="requires the payload digest"):
        ExecAllocation(
            request=uuid4(),
            domain=uuid4(),
            request_digest="b" * 64,
            deadline=datetime.now(UTC),
            digest_version=2,
        )


def test_matching_payload_is_recorded_without_evidence(tmp_path: Path) -> None:
    payload = b'{"v": 1, "code": "print(1)"}'
    allocation, agent_id, generation, owner = _version_2(payload=payload)
    request = _domain_request(tmp_path, allocation, payload)

    observation = observe_exec_payload(
        allocation,
        request_path=request,
        agent_id=agent_id,
        generation=generation,
        runtime_owner=owner,
        phase="settle",
    )

    assert observation.matched
    assert not observation.refused
    assert not (request.parent.parent.parent / "evidence").exists()


def test_a_version_1_row_still_refuses_a_rewritten_payload(tmp_path: Path) -> None:
    allocation = _version_1(payload=b"first")
    request = _domain_request(tmp_path, allocation, b"second")

    observation = observe_exec_payload(
        allocation,
        request_path=request,
        agent_id=9,
        generation=uuid4(),
        runtime_owner=uuid4(),
        phase="settle",
    )

    assert not observation.matched
    assert observation.authoritative
    assert observation.refused
    record = json.loads(
        (request.parent.parent.parent / "evidence" / f"{allocation.request}.json").read_text()
    )
    assert record["refused"] is True
    assert record["digest_version"] == 1
    assert record["condition"] == "changed"


def test_a_version_2_row_reports_a_rewritten_payload_without_refusing(
    tmp_path: Path,
) -> None:
    payload = b'{"v": 1, "code": "print(1)"}'
    allocation, agent_id, generation, owner = _version_2(payload=payload)
    request = _domain_request(tmp_path, allocation, b'{"v": 1, "code": "scrub()"}')

    observation = observe_exec_payload(
        allocation,
        request_path=request,
        agent_id=agent_id,
        generation=generation,
        runtime_owner=owner,
        phase="settle",
    )

    assert not observation.matched
    assert not observation.authoritative
    assert not observation.refused
    record = json.loads(
        (request.parent.parent.parent / "evidence" / f"{allocation.request}.json").read_text()
    )
    assert record["refused"] is False
    assert record["condition"] == "changed"
    assert record["recorded_payload_sha256"] == allocation.request_sha256
    assert record["observed_payload_sha256"] != allocation.request_sha256
    assert record["payload_path"] == str(request)


def test_an_identity_digest_that_no_longer_derives_raises(tmp_path: Path) -> None:
    allocation, agent_id, _generation, owner = _version_2(payload=b"payload")
    request = _domain_request(tmp_path, allocation, b"payload")

    with pytest.raises(ResourceEvidenceError, match="identity differs"):
        observe_exec_payload(
            allocation,
            request_path=request,
            agent_id=agent_id,
            generation=uuid4(),
            runtime_owner=owner,
            phase="settle",
        )


class _RecordingConnection:
    def __init__(self, *, resources: dict[str, Any], generation: UUID, owner: UUID) -> None:
        self.resources = resources
        self.generation = generation
        self.owner = owner
        self.info = SimpleNamespace(transaction_status=TransactionStatus.INTRANS)
        self.stores: list[dict[str, Any]] = []
        self._query = ""

    def execute(self, query: str, params: Any = None) -> _RecordingConnection:
        self._query = query
        if query.lstrip().startswith("UPDATE"):
            self.stores.append(params[0].obj)
        return self

    def fetchone(self) -> tuple[Any, ...]:
        if "SELECT incarnation_resources,runtime_generation,runtime_owner,machine" in self._query:
            return (self.resources, self.generation, self.owner, "test-machine")
        if "SELECT incarnation_resources,machine,runtime_generation,runtime_owner" in self._query:
            return (self.resources, "test-machine", self.generation, self.owner, None)
        return (self.generation, self.owner, self.resources, "running", None)


class _RecordingDatabase:
    def __init__(self, connection: _RecordingConnection) -> None:
        self.connection = connection

    def write_transaction(self) -> Any:
        return nullcontext(self.connection)


def _closed_domain(
    tmp_path: Path,
    allocation: ExecAllocation,
    *,
    payload: bytes,
    agent_id: int,
    generation: UUID,
    owner: UUID,
) -> ExecAllocation:
    attached = allocation.model_copy(
        update={"owner_process": _process(41), "root_process": _process(42)}
    )
    root = exec_run_dir(Path(tmp_path)) / str(agent_id) / "domains" / str(allocation.request)
    root.mkdir(parents=True)
    request = root / f"req-{allocation.request.hex}.json"
    request.write_bytes(payload)
    context = OwnerContext(
        agent_id=agent_id,
        generation=generation,
        runtime_owner=owner,
        request_path=request.resolve(),
        result_path=(root / "result.json").resolve(),
        allocation=allocation,
    )
    (root / "owner.json").write_text(context.model_dump_json())
    (root / "owner.closed").write_text(
        OwnerClosed(
            allocation=attached,
            root_exit_code=0,
            reason="completed",
            observed_at=datetime.now(UTC),
        ).model_dump_json()
    )
    return attached


def test_recovery_discharges_a_rewritten_version_2_allocation(tmp_path: Path) -> None:
    generation, owner = uuid4(), uuid4()
    payload = b'{"v": 1, "code": "print(1)"}'
    allocation, agent_id, _generation, _owner = _version_2(
        payload=payload, generation=generation, owner=owner
    )
    attached = _closed_domain(
        tmp_path,
        allocation,
        payload=b'{"v": 1, "code": "scrub()"}',
        agent_id=agent_id,
        generation=generation,
        owner=owner,
    )
    state = IncarnationResources(
        generation=generation,
        owner=owner,
        host_process=_process(1),
        requests={str(attached.request): attached},
    )
    connection = _RecordingConnection(
        resources=state.model_dump(mode="json"), generation=generation, owner=owner
    )

    recover_local_resources(
        cast(Database, _RecordingDatabase(connection)),
        agent_id,
        "test-machine",
        home=tmp_path,
    )

    assert connection.stores, "the rewritten-envelope allocation must still be discharged"
    assert connection.stores[-1]["requests"] == {}


def test_recovery_still_refuses_a_rewritten_version_1_allocation(tmp_path: Path) -> None:
    allocation = _version_1(payload=b"first")
    generation, owner = uuid4(), uuid4()
    attached = _closed_domain(
        tmp_path,
        allocation,
        payload=b"second",
        agent_id=9,
        generation=generation,
        owner=owner,
    )
    state = IncarnationResources(
        generation=generation,
        owner=owner,
        host_process=_process(1),
        requests={str(attached.request): attached},
    )
    connection = _RecordingConnection(
        resources=state.model_dump(mode="json"), generation=generation, owner=owner
    )

    with pytest.raises(ResourceEvidenceError, match="recovery receipt differs"):
        recover_local_resources(
            cast(Database, _RecordingDatabase(connection)),
            9,
            "test-machine",
            home=tmp_path,
        )
    assert not connection.stores


def _condition(request: Path, condition: str) -> None:
    if condition == "missing":
        request.unlink()
    elif condition == "unreadable":
        request.chmod(0o000)
    elif condition == "symlink":
        target = request.with_name("elsewhere.json")
        target.write_bytes(b"{}")
        request.unlink()
        request.symlink_to(target)
    elif condition == "oversize":
        with request.open("r+b") as stream:
            stream.truncate(MAX_PAYLOAD_BYTES + 1)
    else:
        raise AssertionError(condition)


_CONDITIONS = ["missing", "unreadable", "symlink", "oversize"]


@pytest.mark.parametrize("condition", _CONDITIONS)
def test_a_version_2_row_observes_every_payload_condition(tmp_path: Path, condition: str) -> None:
    payload = b'{"v": 1, "code": "print(1)"}'
    allocation, agent_id, generation, owner = _version_2(payload=payload)
    request = _domain_request(tmp_path, allocation, payload)
    _condition(request, condition)
    records: list[Any] = []
    sink = logger.add(
        lambda message: records.append(message.record), level="ERROR", format="{message}"
    )
    try:
        observation = observe_exec_payload(
            allocation,
            request_path=request,
            agent_id=agent_id,
            generation=generation,
            runtime_owner=owner,
            phase="settle",
        )
    finally:
        logger.remove(sink)

    assert observation.condition == condition
    assert observation.observed_sha256 is None
    assert observation.refused is False
    record = json.loads(
        (request.parent.parent.parent / "evidence" / f"{allocation.request}.json").read_text()
    )
    assert record["condition"] == condition
    assert record["refused"] is False
    assert record["observed_payload_sha256"] is None
    assert [entry["extra"]["condition"] for entry in records] == [condition]
    assert records[0]["extra"]["refused"] is False


@pytest.mark.parametrize("condition", _CONDITIONS)
def test_a_version_1_row_still_refuses_every_payload_condition(
    tmp_path: Path, condition: str
) -> None:
    payload = b"first"
    allocation = _version_1(payload=payload)
    request = _domain_request(tmp_path, allocation, payload)
    _condition(request, condition)

    observation = observe_exec_payload(
        allocation,
        request_path=request,
        agent_id=9,
        generation=uuid4(),
        runtime_owner=uuid4(),
        phase="settle",
    )

    assert observation.condition == condition
    assert observation.refused is True


def test_recovery_discharges_a_version_2_allocation_whose_envelope_is_gone(
    tmp_path: Path,
) -> None:
    generation, owner = uuid4(), uuid4()
    payload = b'{"v": 1}'
    allocation, agent_id, _generation, _owner = _version_2(
        payload=payload, generation=generation, owner=owner
    )
    attached = _closed_domain(
        tmp_path,
        allocation,
        payload=payload,
        agent_id=agent_id,
        generation=generation,
        owner=owner,
    )
    (
        exec_run_dir(tmp_path)
        / str(agent_id)
        / "domains"
        / str(allocation.request)
        / f"req-{allocation.request.hex}.json"
    ).unlink()
    state = IncarnationResources(
        generation=generation,
        owner=owner,
        host_process=_process(1),
        requests={str(attached.request): attached},
    )
    connection = _RecordingConnection(
        resources=state.model_dump(mode="json"), generation=generation, owner=owner
    )

    recover_local_resources(
        cast(Database, _RecordingDatabase(connection)),
        agent_id,
        "test-machine",
        home=tmp_path,
    )

    assert connection.stores and connection.stores[-1]["requests"] == {}
