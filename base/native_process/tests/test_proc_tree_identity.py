from __future__ import annotations

import dataclasses
import errno
import os
import signal
import subprocess
import sys

import psutil
import pytest

from base.native_process import ownership as proc_tree
from base.native_process import pidfd
from base.native_process.kernel_identity import native_boot_id
from base.native_process.ownership import OwnedProcess


@pytest.mark.parametrize(
    ("left", "right", "same"),
    [
        (OwnedProcess(41, 100.0, 80), OwnedProcess(41, 101.0, 80), True),
        (OwnedProcess(41, 100.0, 80), OwnedProcess(41, 100.0, 81), False),
        (OwnedProcess(41, 100.0, 80), OwnedProcess(42, 100.0, 80), False),
        (OwnedProcess(41, 100.0, 80), OwnedProcess(41, 100.0, None), False),
        (OwnedProcess(41, 100.0, None), OwnedProcess(41, 100.0, 80), False),
        (OwnedProcess(41, 100.0, None), OwnedProcess(41, 100.0, None), True),
        (OwnedProcess(41, 100.0, None), OwnedProcess(41, 100.01, None), False),
    ],
)
@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_same_birth(
    left: OwnedProcess,
    right: OwnedProcess,
    same: bool,
    platform: str,
) -> None:
    if platform == "linux" and (left.starttime is None or right.starttime is None):
        same = False
    assert left.same_birth(right, platform=platform) is same
    assert right.same_birth(left, platform=platform) is same


def test_missing_linux_ticks_never_authorize_live_or_capture() -> None:
    identity = OwnedProcess(os.getpid(), 1.0, None)
    with pytest.raises(RuntimeError, match="Linux start ticks"):
        identity.live(platform="linux", observed_ticks=(None,))
    with pytest.raises(RuntimeError, match="Linux start ticks"):
        identity.birth_key(platform="linux")
    with pytest.raises(RuntimeError, match="Linux start ticks"):
        OwnedProcess.capture(psutil.Process(), platform="linux", observed_ticks=(None,))


def test_lost_second_tick_read_is_unknown_for_existing_pid() -> None:
    with pytest.raises(RuntimeError, match="cannot finish Linux start ticks capture"):
        OwnedProcess.capture(
            psutil.Process(),
            platform="linux",
            observed_ticks=(123, None),
        )


@pytest.mark.parametrize("ticks", [0, -1, True])
def test_invalid_current_ticks_never_prove_native_exit(ticks: int) -> None:
    identity = OwnedProcess(os.getpid(), 1.0, 123)
    with pytest.raises(RuntimeError, match="invalid Linux start ticks"):
        identity.live(platform="linux", observed_ticks=(ticks,))
    with pytest.raises(RuntimeError, match="invalid Linux start ticks"):
        OwnedProcess.capture(psutil.Process(), platform="linux", observed_ticks=(ticks,))


def test_native_signal_keeps_exact_birth_and_preserves_sibling() -> None:
    children = [subprocess.Popen(["/bin/sleep", "30"]) for _ in range(2)]
    try:
        owner = OwnedProcess.capture(psutil.Process(children[0].pid))
        sibling = OwnedProcess.capture(psutil.Process(children[1].pid))
        if sys.platform == "linux":
            owner = dataclasses.replace(owner, birth=owner.birth - 3600)
        assert owner.send_signal(signal.SIGTERM)
        assert children[0].wait(timeout=5) == -signal.SIGTERM
        assert sibling.live()
        assert not owner.send_signal(signal.SIGTERM)
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


@pytest.mark.parametrize("operation", ["open", "signal"])
@pytest.mark.parametrize(
    ("code", "error"),
    [(errno.ESRCH, ProcessLookupError), (errno.EPERM, PermissionError), (errno.ENOSYS, OSError)],
)
def test_native_pidfd_errno_remains_an_authority_failure(
    operation: str, code: int, error: type[OSError]
) -> None:
    call = "pidfd_open(41)" if operation == "open" else "pidfd_send_signal(70)"
    with pytest.raises(error) as failure:
        pidfd.check_pidfd_result(-1, code, call=call)
    assert failure.value.errno == code


def test_missing_libc_pidfd_support_refuses() -> None:
    with pytest.raises(RuntimeError, match="requires libc pidfd support"):
        pidfd.require_pidfd_support(platform="linux", has_symbols=False)


@pytest.mark.requires_host("linux")
def test_native_descriptor_retains_exited_task_without_inheritance() -> None:
    child = subprocess.Popen(["/bin/sleep", "30"])
    descriptor = pidfd.open_process(child.pid)
    try:
        assert not os.get_inheritable(descriptor)
        pidfd.send_signal(descriptor, signal.SIGTERM)
        assert child.wait(timeout=5) == -signal.SIGTERM
        with pytest.raises(ProcessLookupError):
            pidfd.send_signal(descriptor, signal.SIGTERM)
    finally:
        os.close(descriptor)
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


def test_native_boot_scope_is_explicit_and_stable() -> None:
    first = native_boot_id()
    assert first == native_boot_id()
    assert first is not None


def test_recapture_retains_original_receipt_per_native_birth() -> None:
    original = OwnedProcess(41, 100.0, 80)
    replacement = OwnedProcess(41, 100.0, 81)
    retained = {original}
    proc_tree.retain_processes(
        retained,
        [OwnedProcess(41, 3700.0, 80), replacement],
        platform="linux",
    )
    assert retained == {original, replacement}
    assert OwnedProcess(41, 3700.0, 80) not in retained


@pytest.mark.parametrize("cached_birth", [None, 1.0])
def test_tree_capture_does_not_adopt_stale_child_listing(
    cached_birth: float | None,
) -> None:
    del cached_birth
    children = [subprocess.Popen(["/bin/sleep", "30"]) for _ in range(2)]
    try:
        parent = OwnedProcess.capture(psutil.Process(children[0].pid))
        unrelated = psutil.Process(children[1].pid)
        captured = proc_tree.capture_tree(parent, candidate_children=[unrelated])
        assert captured == {parent}
        assert children[1].poll() is None
    finally:
        for child in children:
            child.kill()
            child.wait(timeout=5)
