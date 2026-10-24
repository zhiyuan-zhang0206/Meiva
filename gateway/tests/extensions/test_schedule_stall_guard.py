# meiva:note
# Schedule stall guards distinguish native parking from unfinished work.
#
# The guard samples the main thread's deepest frame; whether the samples read as a stall is the
# pure verdict of `stall_policy` / `guard_admission`. These tests observe real frames: a guard
# that never reaches its budget is built on the test thread, a second thread samples it while the
# test thread does the work under observation, and the samples are what the guard would have judged.
# The hard exit and everything after it run in a real runner process
# (`tests/integration/test_schedule_runner_cleanup.py`).

from __future__ import annotations

import selectors
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress

import pytest

from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from services.wake.schedule_manager import engine
from services.wake.schedule_manager.stall_policy import Signature, StallPolicy

_NEVER_STALLS = StallPolicy(timeout_s=3600.0)


# meiva:note
# What the guard sees of this thread while the caller works.
@contextmanager
def _samples(
    database_gate: ProcessDbGate, *, patch_sleep: bool = False
) -> Generator[list[Signature | None]]:
    if patch_sleep:
        engine.patch_park_detection()
    guard = engine.StallGuard(
        Database.from_settings(gate=database_gate), 1, None, policy=_NEVER_STALLS
    )
    seen: list[Signature | None] = []
    done = threading.Event()

    def poll() -> None:
        while not done.is_set():
            seen.append(guard.sample())
            done.wait(0.01)

    poller = threading.Thread(target=poll)
    poller.start()
    try:
        yield seen
    finally:
        done.set()
        poller.join()
        guard.close()
        if patch_sleep:
            engine.restore_park_detection()


def _stood_on(seen: list[Signature | None], function: str) -> bool:
    return any(sample is not None and sample[2] == function for sample in seen)


def test_a_busy_main_thread_is_observed_as_standing_on_a_frame(
    database_gate: ProcessDbGate,
) -> None:
    with _samples(database_gate) as seen:
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            # meiva:note
            # a stable, non-park frame for the guard to observe
            pass
    assert any(sample is not None for sample in seen)


# meiva:note
# A resident schedule parks in time.sleep between fire windows — that is
# the design, not a stall. The runner wraps time.sleep before running a script;
# without the wrapper the deepest Python frame of a sleeping main thread is
# sleep's *caller*, which is indistinguishable from a stall.
def test_a_legitimately_sleeping_main_thread_is_never_observed(
    database_gate: ProcessDbGate,
) -> None:
    with _samples(database_gate, patch_sleep=True) as seen:
        # meiva:note
        # main thread parked in time.sleep the whole time
        time.sleep(0.3)
    assert seen
    assert all(sample is None for sample in seen)


# meiva:note
# Locate the wait independently of the guard's verdict; spawn and argument conversion are outside it.
def _in_child_wait(child: subprocess.Popen[bytes], thread_id: int) -> bool:
    frame = sys._current_frames().get(thread_id)
    if frame is None:
        return False
    code = frame.f_code
    if not (
        (
            code.co_filename == subprocess.Popen[bytes].communicate.__code__.co_filename
            and code.co_name == "_wait"
        )
        or (
            code.co_filename == selectors.SelectSelector.select.__code__.co_filename
            and code.co_name == "select"
        )
    ):
        return False
    while frame is not None:
        if frame.f_code is subprocess.Popen[bytes].communicate.__code__:
            return frame.f_locals.get("self") is child
        frame = frame.f_back
    return False


# meiva:note
# The real child cannot exit until this observer releases its socket latch, including on failure.
def _sample_held_child_wait(
    guard: engine.StallGuard, child: subprocess.Popen[bytes], latch: socket.socket
) -> list[Signature | None]:
    try:
        deadline = time.monotonic() + 2
        while not _in_child_wait(child, guard.main_thread_id):
            if time.monotonic() >= deadline:
                raise AssertionError("the main thread never entered its bounded child wait")
            time.sleep(0.001)
        assert child.poll() is None
        seen: list[Signature | None] = []
        for _ in range(5):
            seen.append(guard.sample())
            time.sleep(0.01)
        assert child.poll() is None
        return seen
    finally:
        with suppress(OSError):
            latch.shutdown(socket.SHUT_WR)


# meiva:note
# Own child startup and every exit path outside the sampling window.
@contextmanager
def _held_child(capture_output: bool) -> Generator[tuple[subprocess.Popen[bytes], socket.socket]]:
    latch, child_latch = socket.socketpair()
    with latch, child_latch:
        latch.settimeout(2)
        # meiva:note
        # Fixed interpreter/program; the only dynamic argument is this test's inherited socket fd.
        with subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                "-c",
                "import socket, sys\n"
                "with socket.socket(fileno=int(sys.argv[1])) as latch:\n"
                "    latch.sendall(b'R')\n"
                "    assert latch.recv(1) == b''\n",
                str(child_latch.fileno()),
            ],
            pass_fds=(child_latch.fileno(),),
            stdout=subprocess.PIPE if capture_output else None,
            stderr=subprocess.PIPE if capture_output else None,
        ) as child:
            try:
                child_latch.close()
                assert latch.recv(1) == b"R"
                yield child, latch
            finally:
                with suppress(OSError):
                    latch.shutdown(socket.SHUT_WR)
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=2)


# meiva:note
# A child wait may outlast the stall budget; its caller owns the timeout.
@pytest.mark.parametrize("capture_output", [True, False], ids=["captured", "uncaptured"])
def test_a_live_child_wait_is_never_observed(
    capture_output: bool, database_gate: ProcessDbGate
) -> None:
    with _held_child(capture_output) as (child, latch):
        guard = engine.StallGuard(
            Database.from_settings(gate=database_gate), 1, None, policy=_NEVER_STALLS
        )
        try:
            with ThreadPoolExecutor(max_workers=1) as observer:
                samples = observer.submit(_sample_held_child_wait, guard, child, latch)
                try:
                    child.communicate(timeout=2)
                finally:
                    with suppress(OSError):
                        latch.shutdown(socket.SHUT_WR)
                    seen = samples.result(timeout=2)
        finally:
            guard.close()
        assert child.returncode == 0
    assert seen
    assert all(sample is None for sample in seen)


# meiva:note
# The select frame used by child waits must still expose socket wedges.
def test_select_outside_subprocess_is_observed(database_gate: ProcessDbGate) -> None:
    import selectors
    import socket

    reader, writer = socket.socketpair()
    with reader, writer, selectors.DefaultSelector() as selector:
        selector.register(reader, selectors.EVENT_READ)
        with _samples(database_gate) as seen:
            selector.select(timeout=0.5)
    assert _stood_on(seen, "select"), "stall guard ignored a socket wait outside subprocess"


# meiva:note
# The caller's timeout does not bound Popen's wait for child startup.
def test_subprocess_spawn_is_observed(database_gate: ProcessDbGate) -> None:

    def slow_spawn() -> None:
        time.sleep(0.5)

    with _samples(database_gate, patch_sleep=True) as seen:
        subprocess.run(
            [sys.executable, "-c", "pass"],
            capture_output=True,
            timeout=2,
            preexec_fn=slow_spawn,
            check=True,
        )
    assert _stood_on(seen, "_execute_child"), "stall guard ignored subprocess spawn"


# meiva:note
# A blocking PathLike callback is not part of the bounded child wait.
def test_subprocess_argument_conversion_is_observed(database_gate: ProcessDbGate) -> None:
    import os
    import socket

    reader, writer = socket.socketpair()
    with reader, writer:
        reader.settimeout(2)

        class SlowExecutable(os.PathLike[str]):
            def __fspath__(self) -> str:
                reader.recv(1)
                return sys.executable

        # meiva:note
        # EOF releases every conversion of the same argument, not just the first.
        wake = threading.Timer(0.5, writer.close)
        with _samples(database_gate, patch_sleep=True) as seen:
            wake.start()
            try:
                # meiva:note
                # test PathLike always resolves to sys.executable.
                subprocess.run(  # noqa: S603
                    [SlowExecutable(), "-c", "pass"],
                    capture_output=True,
                    timeout=2,
                    check=True,
                )
            finally:
                wake.cancel()
                wake.join()
    assert _stood_on(seen, "__fspath__"), "stall guard ignored subprocess argument conversion"


# meiva:note
# communicate's entry flush can block before it checks its deadline.
def test_subprocess_stdin_flush_is_observed(database_gate: ProcessDbGate) -> None:
    import os

    with subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(2)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        bufsize=8192,
    ) as child:
        assert child.stdin is not None
        fd = child.stdin.fileno()
        os.set_blocking(fd, False)
        try:
            with suppress(BlockingIOError):
                while True:
                    os.write(fd, b"x" * 65536)
        finally:
            os.set_blocking(fd, True)
        # meiva:note
        # Leave one byte in BufferedWriter.
        assert child.stdin.write(b"x") == 1

        wake = threading.Timer(0.5, child.terminate)
        with _samples(database_gate, patch_sleep=True) as seen:
            wake.start()
            try:
                with pytest.raises(subprocess.TimeoutExpired):
                    child.communicate(timeout=0.01)
            finally:
                wake.cancel()
                wake.join()
                child.kill()
                child.wait(timeout=2)
    assert _stood_on(seen, "_communicate"), "stall guard ignored subprocess stdin.flush"


# meiva:note
# A selector in argument conversion is not communicate's selector wait.
def test_a_selector_in_argument_conversion_is_observed(database_gate: ProcessDbGate) -> None:
    import os
    import selectors
    import socket

    reader, writer = socket.socketpair()
    with reader, writer, selectors.DefaultSelector() as selector:
        selector.register(reader, selectors.EVENT_READ)

        class SlowExecutable(os.PathLike[str]):
            def __fspath__(self) -> str:
                selector.select(timeout=2)
                return sys.executable

        # meiva:note
        # EOF makes every conversion ready; the timeout bounds a broken timer.
        wake = threading.Timer(0.5, writer.close)
        with _samples(database_gate, patch_sleep=True) as seen:
            wake.start()
            try:
                # meiva:note
                # test PathLike always resolves to sys.executable.
                subprocess.run(  # noqa: S603
                    [SlowExecutable(), "-c", "pass"],
                    capture_output=True,
                    timeout=2,
                    check=True,
                )
            finally:
                wake.cancel()
                wake.join()
    assert _stood_on(seen, "select"), "stall guard ignored a selector in argument conversion"
