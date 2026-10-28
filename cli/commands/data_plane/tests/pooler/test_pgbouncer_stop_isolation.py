# meiva:note
# The pooler paths only ever signal a pid they have confirmed is their pooler.
#
# The pidfile holds a bare pid, and a stale one, left behind whenever the pooler is SIGKILLed
# rather than allowed to unlink it, eventually names a recycled process. Untreated that wedges
# the cluster: bring-up reads the live number as "already running", SIGHUPs a stranger, starts
# nothing and never rewrites the pidfile, so every later start repeats it. These pin the
# ownership check on the stop path against real processes, and on the substring trap that
# would let one home claim another's. The start path's refusal of a recycled pid is pinned in
# `test_pgbouncer_ensure.py`.

from __future__ import annotations

import os
from pathlib import Path

import pytest

from base.clock import SYSTEM_TIMERS
from base.paths import meiva_home
from cli.commands.data_plane import pgbouncer as pooler
from cli.commands.data_plane.tests.native_processes import running_pooler


def _write_pidfile(home: Path, pid: int) -> Path:
    pidfile = home / "pgbouncer" / "pgbouncer.pid"
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(str(pid))
    return pidfile


# meiva:note
# The pidfile names a live process that is not our pooler. This test's own interpreter is the
# stranger: a real, live, definitively not-pgbouncer pid, so nothing about the check is replaced.
def test_stop_does_not_signal_a_recycled_pid(
    unit_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pidfile = _write_pidfile(unit_home, os.getpid())

    assert pooler.stop_pgbouncer(home=meiva_home(), timers=SYSTEM_TIMERS) == "not-running"

    assert not pidfile.exists(), "the stale pidfile must be repaired, not re-read next time"
    assert f"pid {os.getpid()}, which is not this home's pooler" in capsys.readouterr().err


def test_a_pidfile_naming_a_process_that_is_gone_is_repaired_silently(
    unit_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pidfile = _write_pidfile(unit_home, 2**22 + 12345)

    assert pooler.running_pid(home=meiva_home()) is None

    assert not pidfile.exists()
    assert capsys.readouterr().err == ""


# meiva:note
# Which of the two command-line shapes the pooler shows decides where the home's config path is
# written: the absolute path in argv (Linux, apt) or the bare file name beside a working
# directory that is the config directory (macOS, brew). Both are launched here whatever the
# platform; the ownership check must read both, and must not over-block: a real pooler still
# gets stopped.
@pytest.mark.parametrize("relative_ini", [False, True])
def test_stop_signals_this_homes_own_pooler(unit_home: Path, relative_ini: bool) -> None:
    with running_pooler(unit_home, relative_ini=relative_ini) as running:
        assert pooler.pid_is_our_pooler(running.pid, home=meiva_home())
        assert pooler.running_pid(home=meiva_home()) == running.pid

        assert pooler.stop_pgbouncer(home=meiva_home(), timers=SYSTEM_TIMERS) == "stopped"

        assert not running.identity.live()


# meiva:note
# `~/.meiva` is a prefix of `~/.meiva-preview`: a substring match on the home path would hand
# the production stop a preview cluster's pooler (and vice versa). The sibling's real pooler
# is what our pidfile names here, as a pidfile copied or shared across homes would.
@pytest.mark.parametrize("relative_ini", [False, True])
def test_a_sibling_home_sharing_a_path_prefix_is_not_ours(
    unit_home: Path, relative_ini: bool
) -> None:
    sibling = Path(f"{unit_home}-preview")
    sibling.mkdir()
    with running_pooler(sibling, relative_ini=relative_ini) as theirs:
        pidfile = _write_pidfile(unit_home, theirs.pid)

        assert not pooler.pid_is_our_pooler(theirs.pid, home=meiva_home())
        assert pooler.stop_pgbouncer(home=meiva_home(), timers=SYSTEM_TIMERS) == "not-running"

        assert not pidfile.exists()
        assert theirs.identity.live(), "the sibling's pooler was not signalled"
        assert (sibling / "pgbouncer" / "pgbouncer.pid").exists()
