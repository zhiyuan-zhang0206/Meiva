from __future__ import annotations

import os
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from base.agents.incarnation.exec_domain_cleanup import (
    AgentResources,
    Verdict,
    agent_resources,
    classify_domain,
    cleanup_pass,
    remove_settled,
    survey_domains,
)
from base.agents.incarnation.resources import ExecAllocation, IncarnationResources
from base.paths import exec_domains_dir

_AGENT = 4242
_OLD = 48 * 3600.0
_THRESHOLD = 24 * 3600.0


def _allocation() -> ExecAllocation:
    return ExecAllocation(
        request=uuid4(), domain=uuid4(), request_digest="0" * 64, deadline=datetime.now(UTC)
    )


def _row(agent_id: int = _AGENT, allocations: Sequence[ExecAllocation] = ()) -> tuple[Any, ...]:
    generation, owner = uuid4(), uuid4()
    state = IncarnationResources(
        generation=generation,
        owner=owner,
        requests={str(allocation.request): allocation for allocation in allocations},
    )
    return (agent_id, generation, owner, state.model_dump(mode="json"))


def _domain(
    home: Path,
    *,
    agent_id: int = _AGENT,
    request_id: UUID | None = None,
    age_s: float = _OLD,
    request_name: str | None = None,
    extra_request: bool = False,
) -> tuple[Path, UUID]:
    identifier = uuid4() if request_id is None else request_id
    directory = exec_domains_dir(home, agent_id) / str(identifier)
    directory.mkdir(parents=True)
    name = f"req-{identifier.hex}.json" if request_name is None else request_name
    (directory / name).write_text("{}")
    (directory / "owner.json").write_text("{}")
    if extra_request:
        (directory / f"req-{uuid4().hex}.json").write_text("{}")
    stamp = time.time() - age_s
    for path in directory.iterdir():
        os.utime(path, (stamp, stamp))
    os.utime(directory, (stamp, stamp))
    return directory, identifier


def _classify(
    directory: Path,
    *,
    stored: AgentResources | None = None,
    live: frozenset[str] = frozenset(),
    min_age_s: float = _THRESHOLD,
) -> Any:
    return classify_domain(
        directory,
        agent_id=_AGENT,
        stored=stored or AgentResources(frozenset(), "unit"),
        live=live,
        min_age_s=min_age_s,
        now=time.time(),
    )


def _survey(home: Path, *, stored: dict[int, AgentResources] | None = None) -> tuple[Any, ...]:
    return survey_domains(
        [_AGENT],
        home=home,
        stored=stored or {_AGENT: AgentResources(frozenset(), "unit")},
        live=frozenset(),
        min_age_s=_THRESHOLD,
    )


def _run(
    home: Path,
    *,
    agent_ids: Sequence[int] = (_AGENT,),
    rows: Sequence[tuple[Any, ...]] = (),
    live: frozenset[str] = frozenset(),
    delete: bool = False,
) -> tuple[int, str]:
    lines: list[str] = []
    status = cleanup_pass(
        list(agent_ids),
        home=home,
        rows=rows,
        live=live,
        delete=delete,
        min_age_s=_THRESHOLD,
        emit=lines.append,
    )
    return status, "\n".join(lines)


def test_settled_directory_is_reported_and_kept_by_default(tmp_path: Path) -> None:
    home = tmp_path / "home"
    directory, _ = _domain(home)

    (entry,) = _survey(home)
    assert entry.verdict is Verdict.SETTLED and entry.disposable

    status, output = _run(home)

    assert status == 0
    assert "1 directories: 1 settled" in output
    assert "report only" in output
    assert directory.exists()


def test_delete_removes_only_settled_directories(tmp_path: Path) -> None:
    home = tmp_path / "home"
    allocation = _allocation()
    settled, _ = _domain(home)
    in_flight, _ = _domain(home, request_id=allocation.request)
    recent, _ = _domain(home, age_s=60.0)
    unknown, _ = _domain(home, request_name="req-other.json")

    status, output = _run(home, rows=[_row(allocations=[allocation])], delete=True)

    assert status == 0
    assert "removed 1 directories" in output
    assert not settled.exists()
    assert in_flight.exists() and recent.exists() and unknown.exists()
    assert "in-flight" in output and "recent" in output and "unknown" in output


def test_allocated_request_is_in_flight(tmp_path: Path) -> None:
    home = tmp_path / "home"
    allocation = _allocation()
    directory, _ = _domain(home, request_id=allocation.request)

    state = agent_resources([_row(allocations=[allocation])])

    (entry,) = _survey(home, stored=state)

    assert entry.verdict is Verdict.IN_FLIGHT
    assert "still holds this request" in entry.detail
    assert remove_settled([entry]).removed == ()
    assert directory.exists()


def test_recent_directory_is_retained(tmp_path: Path) -> None:
    directory, _ = _domain(tmp_path / "home", age_s=60.0)

    entry = _classify(directory)

    assert entry.verdict is Verdict.RECENT
    assert "under the 24.0h threshold" in entry.detail
    assert not entry.disposable


@pytest.mark.parametrize("variant", ["not-a-request-id", "renamed", "extra", "symlink"])
def test_directory_outside_the_written_shape_is_unknown(tmp_path: Path, variant: str) -> None:
    home = tmp_path / "home"
    if variant == "not-a-request-id":
        directory = exec_domains_dir(home, _AGENT) / "not-a-request-id"
        directory.mkdir(parents=True)
        (directory / "req-x.json").write_text("{}")
    elif variant == "renamed":
        directory, _ = _domain(home, request_name="req-other.json")
    elif variant == "extra":
        directory, _ = _domain(home, extra_request=True)
    else:
        target, _ = _domain(home)
        directory = exec_domains_dir(home, _AGENT) / "linked"
        directory.symlink_to(target)

    entry = _classify(directory, min_age_s=0.0)

    assert entry.verdict is Verdict.UNKNOWN
    assert not entry.disposable


def test_unreadable_resource_set_retains_every_directory(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _domain(home)

    state = agent_resources([(_AGENT, uuid4(), uuid4(), {"state": "nonsense"})])

    (entry,) = _survey(home, stored=state)

    assert entry.verdict is Verdict.UNKNOWN
    assert "unreadable" in entry.detail


def test_foreign_incarnation_resource_set_proves_nothing() -> None:
    allocation = _allocation()
    generation, owner = uuid4(), uuid4()
    state = IncarnationResources(
        generation=generation,
        owner=owner,
        requests={str(allocation.request): allocation},
    )

    parsed = agent_resources([(_AGENT, uuid4(), owner, state.model_dump(mode="json"))])

    assert parsed[_AGENT].unsettled is None
    assert "another incarnation" in parsed[_AGENT].detail


def test_birth_marker_row_has_no_unsettled_requests() -> None:
    birth = {"state": "unadmitted", "birth": str(uuid4())}

    parsed = agent_resources([(_AGENT, uuid4(), uuid4(), birth)])

    assert parsed[_AGENT].unsettled == frozenset()


def test_live_process_reference_retains_the_directory(tmp_path: Path) -> None:
    home = tmp_path / "home"
    directory, identifier = _domain(home)
    request = directory / f"req-{identifier.hex}.json"

    entry = _classify(directory, live=frozenset({os.path.realpath(request)}))

    assert entry.verdict is Verdict.IN_FLIGHT
    assert "live process" in entry.detail


def test_all_agents_covers_every_agent_directory(tmp_path: Path) -> None:
    home = tmp_path / "home"
    first, _ = _domain(home, agent_id=11)
    second, _ = _domain(home, agent_id=12)

    status, output = _run(home, agent_ids=(11, 12), delete=True)

    assert status == 0
    assert "agent 11" in output and "agent 12" in output
    assert not first.exists() and not second.exists()


def test_removal_failure_is_reported(tmp_path: Path) -> None:
    home = tmp_path / "home"
    directory, _ = _domain(home)

    def refuse(_path: Path) -> None:
        raise OSError("read-only home")

    (entry,) = _survey(home)

    report = remove_settled([entry], remove=refuse)

    assert report.removed == () and report.freed_bytes == 0
    assert report.failures == ((entry, "read-only home"),)
    assert directory.exists()


def test_missing_agent_row_leaves_nothing_to_prove_in_flight(tmp_path: Path) -> None:
    home = tmp_path / "home"
    directory, _ = _domain(home)

    (entry,) = _survey(home, stored={})

    assert entry.verdict is Verdict.SETTLED
    assert remove_settled([entry]).removed == (entry,)
    assert not directory.exists()


def test_empty_home_reports_nothing_to_do(tmp_path: Path) -> None:
    status, output = _run(tmp_path / "home", agent_ids=())

    assert status == 0
    assert "no agent run/exec directory" in output
