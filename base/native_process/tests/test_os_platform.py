"""Unit tests for base.native_process.os_platform — host detection + the disk-path probe.

The primary-disk-path probe samples the macOS data volume or the POSIX root.
"""

from __future__ import annotations

import io
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, cast

import pytest

from base.native_process.os_platform import (
    Platform,
    crontab_lock,
    descends_from_launchd_job,
    ensure_line_buffered_stdio,
    primary_disk_path,
    pty_max,
)

_CHILD = Path(__file__).with_name("_os_platform_child.py")


def test_cold_import_does_not_probe_uname() -> None:
    result = subprocess.run(  # noqa: S603 -- fixed argv, the venv interpreter runs the child script
        [
            ".venv/bin/python",
            "-I",
            str(_CHILD),
            "cold_import_does_not_probe_uname",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


class TestPrimaryDiskPath:
    def test_macos_data_volume(self) -> None:
        assert primary_disk_path(macos=True) == "/System/Volumes/Data"

    def test_plain_posix_root(self) -> None:
        assert primary_disk_path(macos=False) == "/"


class TestPtyMax:
    def test_non_macos_returns_none(self) -> None:
        """Off macOS the PTY ceiling does not bind (Linux `kernel.pty.max` is far
        higher), so callers get None and skip the check."""
        assert pty_max(macos=False) is None

    @pytest.mark.requires_host("darwin")
    def test_macos_reads_positive_ceiling(self) -> None:
        """On macOS it returns the live `kern.tty.ptmx_max` — a positive int
        (511 by default)."""
        value = pty_max()
        assert isinstance(value, int)
        assert value > 0


class TestEnsureLineBufferedStdio:
    def test_pipe_stdout_streams_before_exit(self) -> None:
        """The regression: a CLI whose stdout is a PIPE block-buffers its own
        `print()` output, so a detached `meiva ... | tee` log shows the parent's lines
        only at exit — after the unbuffered output of every child it spawned. Run a
        child that prints and then blocks, and assert the line is readable while it
        is still alive."""
        repo_root = str(Path(__file__).resolve().parents[3])
        proc = subprocess.Popen(  # noqa: S603 — this interpreter, a fixed child script
            [sys.executable, str(_CHILD), "pipe_stdout_streams_before_exit", repo_root],
            stdout=subprocess.PIPE,
            text=True,
        )
        # The read must be TIME-bounded, not just eventual: without the fix the line
        # does arrive — at exit, 30s later, which is exactly the bug (the parent's
        # lines land after everything its children streamed). A plain blocking
        # readline would therefore pass on the broken build, just slowly.
        assert proc.stdout is not None
        got: list[str] = []
        reader = threading.Thread(target=lambda: got.append(proc.stdout.readline()))  # type: ignore[union-attr]
        reader.daemon = True
        reader.start()
        reader.join(timeout=10.0)
        proc.kill()
        proc.wait(timeout=10)
        assert got, "the child's line did not reach the pipe while it was still running"
        assert got[0].strip() == "[update] header"

    def test_idempotent_and_survives_a_stream_without_reconfigure(self) -> None:
        """Buffering is a nicety: a replaced stdout (pytest capture, a StringIO under
        redirect_stdout) must never take the command down."""
        stream = io.StringIO()
        ensure_line_buffered_stdio(streams=(stream, sys.stderr))
        ensure_line_buffered_stdio(streams=(stream, sys.stderr))


# meiva:note
# The crontab lock lives in the account home the caller names, so a converge that already read that
# home decides where the lock is. A reader that fell back to its own `~` would lock another file.
def test_crontab_lock_holds_the_file_under_the_home_it_is_given(tmp_path: Path) -> None:
    home = tmp_path / "account"
    home.mkdir()

    with crontab_lock(user_home=home):
        locked = home / ".meiva-crontab.lock"
        assert locked.is_file()

    assert locked.read_text() == ""


class TestLaunchdOwnership:
    def _check(
        self,
        *,
        print_rc: int,
        print_out: str,
        ps: dict[int, int | None],
    ) -> bool:
        return descends_from_launchd_job(
            "com.x",
            uid=501,
            macos=True,
            current_pid=500,
            launchctl_print=(print_rc, print_out),
            parent_pids=ps,
        )

    def test_off_macos_is_never_owned(self) -> None:
        assert descends_from_launchd_job("com.x", uid=501, macos=False) is False

    def test_unloaded_job_owns_no_process(self) -> None:
        assert self._check(print_rc=113, print_out="Could not find service", ps={}) is False

    def test_loaded_but_not_running_owns_no_process(self) -> None:
        assert self._check(print_rc=0, print_out="state = not running\n", ps={}) is False

    def test_ancestor_chain_proves_ownership(self) -> None:
        assert self._check(print_rc=0, print_out="\tpid = 200\n", ps={500: 300, 300: 200}) is True

    def test_chain_reaching_launchd_is_external(self) -> None:
        assert self._check(print_rc=0, print_out="pid = 999\n", ps={500: 300, 300: 1}) is False

    def test_unreadable_ancestor_is_not_proof(self) -> None:
        assert self._check(print_rc=0, print_out="pid = 200\n", ps={500: 300}) is False


# -- Platform: the facts a Host reports, validated at construction --


@pytest.mark.parametrize(
    ("sys_platform", "system"),
    [("darwin", "darwin"), ("linux", "linux"), ("linux2", "linux")],
)
def test_a_supported_sys_platform_reduces_to_linux_or_darwin(
    sys_platform: str, system: str
) -> None:
    built = Platform.from_sys_platform(sys_platform, machine="arm64", executable="/usr/bin/python")
    assert built.system == system
    assert built == Platform(cast("Any", system), "arm64", "/usr/bin/python")


@pytest.mark.parametrize("sys_platform", ["win32", "cygwin", "freebsd14", ""])
def test_an_unsupported_sys_platform_is_refused(sys_platform: str) -> None:
    with pytest.raises(ValueError, match="unsupported platform"):
        Platform.from_sys_platform(sys_platform, machine="x86_64", executable="/usr/bin/python")


@pytest.mark.parametrize(
    ("system", "machine", "executable", "message"),
    [
        ("windows", "x86_64", "/usr/bin/python", "system"),
        ("linux", "", "/usr/bin/python", "machine"),
        ("linux", "x86_64", "", "executable"),
    ],
)
def test_a_platform_rejects_values_it_cannot_describe(
    system: str, machine: str, executable: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        Platform(cast("Any", system), machine, executable)
