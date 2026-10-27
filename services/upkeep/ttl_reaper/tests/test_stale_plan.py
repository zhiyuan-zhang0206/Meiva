from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from services.upkeep.ttl_reaper import stale_plan
from services.upkeep.ttl_reaper.shell_plan import UNKNOWN_MACHINE, ShellRow

_EXPIRED = datetime(2026, 10, 20, 9, 30, tzinfo=UTC)


def _shell(agent: int, session: int, machine: str | None) -> ShellRow:
    return ShellRow(agent, session, machine, _EXPIRED, _EXPIRED - timedelta(hours=1))


def test_stale_rows_group_by_machine_then_agent_in_arrival_order() -> None:
    rows = [_shell(1, 1, "a"), _shell(1, 2, "a"), _shell(2, 3, "a"), _shell(1, 4, "b")]

    plan = stale_plan.plan_stale(rows)

    assert [
        (machine.machine, [(a.agent_id, [r.session_id for r in a.rows]) for a in machine.agents])
        for machine in plan.machines
    ] == [
        ("a", [(1, [1, 2]), (2, [3])]),
        ("b", [(1, [4])]),
    ]
    assert plan.unplaced == ()


@pytest.mark.parametrize("machine", [None, "", UNKNOWN_MACHINE])
def test_a_stale_row_without_a_usable_machine_is_set_aside(machine: str | None) -> None:
    plan = stale_plan.plan_stale([_shell(1, 1, machine), _shell(1, 2, "a")])

    assert [probes.machine for probes in plan.machines] == ["a"]
    assert [row.session_id for row in plan.unplaced] == [1]


def test_an_empty_stale_plan_probes_nothing() -> None:
    plan = stale_plan.plan_stale([])

    assert plan.machines == () and plan.unplaced == ()


def test_a_probe_answer_reads_the_listed_session_ids() -> None:
    assert stale_plan.probe_session_ids(
        {"shells": [{"id": 3, "name": "build"}, {"id": 4, "name": "watcher"}]}
    ) == frozenset({3, 4})


def test_an_empty_listing_is_a_real_answer() -> None:
    assert stale_plan.probe_session_ids({"shells": []}) == frozenset()


@pytest.mark.parametrize(
    "answer",
    [
        {},
        {"shells": None},
        {"shells": {"id": 3}},
        {"shells": [3]},
        {"shells": [{"id": "3"}]},
        {"shells": [{"name": "build"}]},
        {"shells": [{"id": True}]},
        {"shells": [{"id": 3}, {}]},
    ],
    ids=[
        "no-shells",
        "null",
        "not-a-list",
        "bare-id",
        "string-id",
        "no-id",
        "bool-id",
        "one-entry-unreadable",
    ],
)
def test_a_malformed_probe_answer_proves_nothing(answer: dict[str, object]) -> None:
    assert stale_plan.probe_session_ids(answer) is None


def test_stale_rows_keep_the_sessions_the_host_did_not_list() -> None:
    rows = [_shell(1, 1, "a"), _shell(1, 2, "a"), _shell(1, 3, "a")]

    assert [r.session_id for r in stale_plan.stale_rows(rows, frozenset({2}))] == [1, 3]
    assert stale_plan.stale_rows(rows, frozenset({1, 2, 3})) == ()
    assert len(stale_plan.stale_rows(rows, frozenset())) == 3
