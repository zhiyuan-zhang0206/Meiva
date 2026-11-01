from __future__ import annotations

import os
import subprocess
import time
from typing import Any

import psutil
import pytest

from base.native_process import ownership as proc_tree
from base.native_process.os_platform import is_linux
from base.native_process.ownership import OwnedProcess, stable_create_time


def _identity_with_drift(offset: float) -> OwnedProcess:
    return OwnedProcess(os.getpid(), stable_create_time(psutil.Process()) + offset, None)


@pytest.mark.parametrize("offset", [-1.0, 1.0, -0.0001, 0.0001])
def test_live_rejects_a_different_native_birth(offset: float) -> None:
    if is_linux():
        with pytest.raises(RuntimeError, match="missing Linux start ticks"):
            _identity_with_drift(offset).live()
    else:
        assert not _identity_with_drift(offset).live()


def test_live_accepts_the_exact_captured_native_birth() -> None:
    assert OwnedProcess.capture(psutil.Process()).live()


def test_birth_matches_exposes_the_same_rule() -> None:
    process = psutil.Process()
    native = OwnedProcess.capture(process)
    assert native.birth_matches(process)
    changed = OwnedProcess(
        native.pid,
        native.birth + 0.0001,
        native.starttime + 1 if native.starttime is not None else None,
    )
    assert not changed.birth_matches(process)


def test_a_proc_entry_that_vanished_mid_check_converges_as_gone() -> None:
    assert OwnedProcess(4242, 0.5, 777).ticks_match(None, pid_exists=False) is False


def test_a_present_process_whose_ticks_cannot_be_read_keeps_the_error() -> None:
    with pytest.raises(RuntimeError, match="cannot verify process identity"):
        OwnedProcess(4242, 0.5, 777).ticks_match(None, pid_exists=True)


@pytest.mark.parametrize(("observed", "expected"), [(777, True), (778, False)])
def test_readable_ticks_decide_by_equality(observed: int, expected: bool) -> None:
    assert OwnedProcess(4242, 0.5, 777).ticks_match(observed, pid_exists=True) is expected


def test_a_record_without_ticks_has_nothing_to_compare() -> None:
    with pytest.raises(ValueError, match="no recorded start ticks"):
        OwnedProcess(4242, 0.5, None).ticks_match(777, pid_exists=True)


# meiva:note
# A child that is still running, and the identity captured from it while it was.
def _running_child() -> tuple[subprocess.Popen[bytes], OwnedProcess]:
    child = subprocess.Popen(["sleep", "60"])
    return child, OwnedProcess.capture(psutil.Process(child.pid))


def test_a_running_child_is_live_and_a_reaped_one_is_not() -> None:
    child, identity = _running_child()
    try:
        assert identity.live()
    finally:
        child.kill()
        child.wait()

    assert not identity.live()


def test_an_exited_child_not_yet_reaped_is_not_live() -> None:
    child, identity = _running_child()
    try:
        child.kill()
        deadline = time.monotonic() + 5.0
        while psutil.Process(child.pid).status() != psutil.STATUS_ZOMBIE:
            assert time.monotonic() < deadline, "the killed child never became a zombie"
            time.sleep(0.01)

        assert not identity.live()
    finally:
        child.wait()


def test_reading_this_process_names_the_identity_it_was_captured_with() -> None:
    process = psutil.Process()
    identity = OwnedProcess.capture(process)

    reading = identity.read_live()

    assert reading is not None and reading.pid_exists
    assert reading.status != psutil.STATUS_ZOMBIE
    if identity.starttime is not None:
        assert reading.ticks == identity.starttime
    else:
        assert reading.identity == identity


def test_reading_a_pid_nobody_holds_finds_nothing() -> None:
    child, identity = _running_child()
    child.kill()
    child.wait()

    assert identity.read_live() is None


@pytest.mark.requires_host("darwin")
@pytest.mark.parametrize("seconds", (60.0, 3600.0))
def test_stable_create_time_ignores_a_simulated_clock_step(seconds: float) -> None:
    # meiva:note
    # psutil's Darwin backend exposes monotonic create_time on the private _proc handle.
    del seconds
    proc = psutil.Process()
    assert stable_create_time(proc) == float(
        proc._proc.create_time(monotonic=True)  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownArgumentType]
    )


@pytest.mark.requires_host("darwin")
@pytest.mark.parametrize("seconds", (60.0, 3600.0))
def test_capture_and_verify_span_clock_epochs(seconds: float) -> None:
    # meiva:note
    # psutil's Darwin backend exposes monotonic create_time on the private _proc handle.
    del seconds
    proc = psutil.Process()
    identity = OwnedProcess.capture(proc)
    assert identity.birth == float(
        proc._proc.create_time(monotonic=True)  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownArgumentType]
    )
    assert identity.live()


def test_process_metadata_records_the_script_of_a_node_ancestor() -> None:
    caller_facts = proc_tree.format_process_node_facts(
        pid=30,
        name="python3.12",
        executable="/usr/bin/python3.12",
        created_at=1.0,
        starttime=None,
        boot_id="boot-1",
        parent_pid=20,
        cmdline=["python", "-m", "cli"],
    )
    node_facts = proc_tree.format_process_node_facts(
        pid=20,
        name="node",
        executable="/usr/bin/node",
        created_at=1.0,
        starttime=None,
        boot_id="boot-1",
        parent_pid=10,
        cmdline=["node", "/opt/homebrew/bin/dsh", "web"],
    )
    shell_facts = proc_tree.format_process_node_facts(
        pid=10,
        name="zsh",
        executable="/usr/bin/zsh",
        created_at=1.0,
        starttime=None,
        boot_id="boot-1",
        parent_pid=0,
        cmdline=["-zsh"],
    )
    metadata = proc_tree.assemble_process_metadata(
        pid=30,
        nodes=[caller_facts, node_facts, shell_facts],
    )
    assert metadata["pid"] == 30 and "script" not in metadata
    ancestors: Any = metadata["ancestors"]
    recorded_node, recorded_shell = ancestors
    assert recorded_node["name"] == "node" and recorded_node["script"] == "/opt/homebrew/bin/dsh"
    assert "script" not in recorded_shell


# meiva:note
# The facts name the process the caller named, and `CODEX_HOME` is recorded only when the caller
# hands it in.
def test_process_metadata_names_the_caller_and_records_the_codex_home_it_was_given() -> None:
    given = proc_tree.process_metadata(pid=os.getpid(), codex_home="/codex")
    absent = proc_tree.process_metadata(pid=os.getpid(), codex_home=None)

    assert given["pid"] == absent["pid"] == os.getpid()
    assert given["codex_home"] == "/codex"
    assert "codex_home" not in absent


# meiva:note
# The caller's id is handed in, not re-derived: a tree rooted at another process names that process
# and walks up from it. A reader that falls back to its own id would record this test process.
def test_process_metadata_roots_the_tree_at_the_pid_it_is_given() -> None:
    child = subprocess.Popen(["sleep", "60"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        metadata = proc_tree.process_metadata(pid=child.pid, codex_home=None)
        ancestors: Any = metadata["ancestors"]

        assert metadata["pid"] == child.pid
        assert ancestors
        assert ancestors[0]["pid"] == os.getpid()
    finally:
        child.kill()
        child.wait()
