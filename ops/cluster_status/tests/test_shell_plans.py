# meiva:note
# Which sessions are an agent's shells, which a kill-all targets and what a kill's outcome is,
# decided from listings and verdicts given as values.

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from base.api_contracts.agent_contract import ShellKillMode
from ops.cluster_status.shell_plans import (
    ListedShell,
    confirm_kill_all,
    find_shell,
    kill_all_targets,
    kill_one_result,
    listed_shells,
)
from ops.rpc_schemas import SessionInfo, ShellInfo


def _sessions(*names: str) -> list[SessionInfo]:
    return [SessionInfo(name=name) for name in names]


def _listed(agent_id: int, *ids: int) -> tuple[ListedShell, ...]:
    return tuple(
        ListedShell(f"meiva-agent-{agent_id}-shell-{i}", ShellInfo(id=i, name=None)) for i in ids
    )


# meiva:note
# An unnamed shell has no name, a watcher is named `watcher`, a named shell carries its slug; the
# agent's main session (no `-shell-`), another agent's shells and non-agent sessions are left out.
def test_only_the_agents_own_shells_are_listed_parsed_and_sorted_by_id() -> None:
    sessions = _sessions(
        "meiva-agent-7",
        "meiva-agent-7-shell-2-watcher",
        "meiva-agent-7-shell-0",
        "meiva-agent-7-shell-1-dev-server",
        "meiva-agent-7-shell-3-page-dashboard",
        "meiva-agent-71-shell-0",
        "meiva-agent-12-shell-3",
        "meiva-restarter",
    )

    listed = listed_shells(sessions, 7)

    assert [(entry.shell.id, entry.shell.name) for entry in listed] == [
        (0, None),
        (1, "dev-server"),
        (2, "watcher"),
        (3, "page-dashboard"),
    ]
    assert [entry.session_name for entry in listed] == [
        "meiva-agent-7-shell-0",
        "meiva-agent-7-shell-1-dev-server",
        "meiva-agent-7-shell-2-watcher",
        "meiva-agent-7-shell-3-page-dashboard",
    ]


def test_a_shell_keeps_the_time_facts_of_its_session() -> None:
    born = datetime(2026, 10, 24, tzinfo=UTC)
    session = SessionInfo(name="meiva-agent-7-shell-0", created_at=born, uptime_seconds=90)

    (entry,) = listed_shells([session], 7)

    assert (entry.shell.created_at, entry.shell.uptime_seconds) == (born, 90)


def test_an_agent_without_shells_lists_nothing() -> None:
    assert listed_shells(_sessions("meiva-agent-7-shell-0"), 99) == ()


def test_a_shell_is_found_by_its_session_id() -> None:
    listed = _listed(7, 0, 2, 5)

    found = find_shell(listed, 2)

    assert found is not None and found.session_name == "meiva-agent-7-shell-2"
    assert find_shell(listed, 3) is None


def test_a_kill_all_takes_every_shell_below_the_cutoff() -> None:
    listed = _listed(7, 0, 2, 5, 6, 8)

    targets = kill_all_targets(listed, 6, frozenset())

    assert [t.shell.id for t in targets] == [0, 2, 5]


def test_a_kill_all_spares_the_pages_a_page_keeps_its_own_lifecycle() -> None:
    listed = _listed(7, 0, 3, 5)

    targets = kill_all_targets(listed, 6, frozenset({3}))

    assert [t.shell.id for t in targets] == [0, 5]


def test_a_kill_all_below_the_first_shell_targets_nothing() -> None:
    assert kill_all_targets(_listed(7, 4, 5), 4, frozenset()) == ()


def test_a_confirmed_kill_all_returns_the_ids_it_removed() -> None:
    targets = _listed(7, 0, 2)

    assert confirm_kill_all(7, targets, [True, True]) == (0, 2)


def test_a_kill_all_names_every_id_the_backend_could_not_confirm() -> None:
    targets = _listed(7, 0, 1, 2)

    with pytest.raises(RuntimeError, match=r"shell session\(s\) \[0, 2\] of agent 7"):
        confirm_kill_all(7, targets, [False, True, False])


def test_verdicts_must_pair_with_targets() -> None:
    with pytest.raises(ValueError, match=r"shorter|longer"):
        confirm_kill_all(7, _listed(7, 0, 1), [True])


@pytest.mark.parametrize("interrupted", [True, False])
def test_a_confirmed_kill_reports_whether_it_cut_work_short(interrupted: bool) -> None:
    (target,) = (ListedShell("meiva-agent-7-shell-3-build", ShellInfo(id=3, name="build")),)

    result = kill_one_result(target, ok=True, interrupted=interrupted)

    assert result == (ShellKillMode.KILLED, interrupted, "build")


def test_an_unconfirmed_kill_is_an_error_not_a_verdict() -> None:
    (target,) = _listed(7, 3)

    with pytest.raises(RuntimeError, match="failed to kill session 'meiva-agent-7-shell-3'"):
        kill_one_result(target, ok=False, interrupted=True)
