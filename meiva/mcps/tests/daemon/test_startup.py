from __future__ import annotations

import pytest

from meiva.mcps._startup import (
    ProcessFacts,
    is_daemon_cmdline,
    refusal_to_start,
    socket_for_args,
    stale_daemon_pids,
)

DEFAULT = "/run/mcp_daemon.sock"


def test_no_argument_binds_the_shared_per_machine_socket() -> None:
    assert socket_for_args([], DEFAULT) == DEFAULT


def test_one_argument_binds_that_path() -> None:
    assert socket_for_args(["/srv/test.sock"], DEFAULT) == "/srv/test.sock"


def test_two_arguments_are_no_launch() -> None:
    assert socket_for_args(["/srv/a.sock", "/srv/b.sock"], DEFAULT) is None


def test_a_live_occupant_forbids_starting_and_a_dead_file_does_not() -> None:
    reason = refusal_to_start("/srv/x.sock", occupied=True)
    assert reason is not None and "/srv/x.sock" in reason and "refusing to start" in reason
    assert refusal_to_start("/srv/x.sock", occupied=False) is None


@pytest.mark.parametrize(
    ("cmdline", "expected"),
    [
        # meiva:note
        # The daemon's own launch shapes.
        ([".venv/bin/python", "-m", "meiva.mcps._daemon"], True),
        ([".venv/bin/python", "-m", "meiva.mcps._daemon", "/srv/x.sock"], True),
        # meiva:note
        # A `bash -lc` wrapper: the whole launch command is ONE argv element that contains the
        # module name; no element equals it, so it is never matched.
        (["bash", "-lc", "cd /root && .venv/bin/python -m meiva.mcps._daemon"], False),
        (
            [
                "bash",
                "-lc",
                "cd /root && export VIRTUAL_ENV=/root/.venv && "
                'export PATH=/root/.venv/bin:"$PATH" && '
                ".venv/bin/python -m meiva.mcps._daemon",
            ],
            False,
        ),
        # meiva:note
        # Same wrapper through `sh -c` (posixproc launches `sh -c "bash -lc ..."`).
        (["/bin/sh", "-c", "bash -lc 'cd /root && .venv/bin/python -m meiva.mcps._daemon'"], False),
        # meiva:note
        # The module name as a plain argument (not after -m) is not a daemon.
        (["python", "-c", "print('meiva.mcps._daemon')"], False),
        (["python", "meiva.mcps._daemon"], False),
        (["meiva.mcps._daemon"], False),
        # meiva:note
        # Unrelated process.
        (["python", "-m", "something.else"], False),
        ([], False),
    ],
)
def test_only_an_argv_shaped_python_dash_m_daemon_is_a_daemon_launch(
    cmdline: list[str], expected: bool
) -> None:
    assert is_daemon_cmdline(cmdline) is expected


DAEMON = (".venv/bin/python", "-m", "meiva.mcps._daemon")
ME = 100
HOME = "/home/unit"
ROOT = "/repo"


def ghosts(*processes: ProcessFacts) -> list[int]:
    return stale_daemon_pids(processes, me=ME, home=HOME, root=ROOT)


# meiva:note
# One ghost runs from this unit's project root, the other with this unit's home.
def test_ghosts_of_this_unit_are_selected_by_root_or_by_home() -> None:
    assert ghosts(
        ProcessFacts(1001, DAEMON, ROOT, ""),
        ProcessFacts(1002, DAEMON, "/elsewhere", HOME),
    ) == [1001, 1002]


# meiva:note
# Never selected: this process itself, a daemon of another unit, a process that is no daemon, and a `bash -lc` wrapper that merely names the module.
def test_this_process_other_units_and_non_daemons_are_never_selected() -> None:
    assert (
        ghosts(
            ProcessFacts(ME, DAEMON, ROOT, HOME),
            ProcessFacts(1003, DAEMON, "/other/root", "/other/home"),
            ProcessFacts(1004, ("python", "-m", "something.else"), ROOT, HOME),
            ProcessFacts(1005, ("bash", "-lc", "x -m meiva.mcps._daemon"), ROOT, HOME),
        )
        == []
    )


def test_an_unset_root_matches_no_working_directory() -> None:
    process = ProcessFacts(1006, DAEMON, "", "")
    assert stale_daemon_pids([process], me=ME, home=HOME, root="") == []
