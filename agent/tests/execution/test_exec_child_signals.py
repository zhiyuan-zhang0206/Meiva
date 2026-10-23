import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent.execution.protocol import (
    make_request_path,
    make_result_path,
    read_result,
    write_request,
)
from agent.tests.execution.test_exec_child import AGENT_ID, child_env
from meiva.tests.fixtures.pin_agent import exec_context


def test_child_sigterm_writes_timed_out_envelope(tmp_path: Path) -> None:
    exec_dir = tmp_path / "exec"
    request_path = make_request_path(exec_dir, agent_id=AGENT_ID)
    result_path = make_result_path(exec_dir, agent_id=AGENT_ID)
    write_request(
        request_path,
        code="import time\nprint('before sleep', flush=True)\ntime.sleep(60)",
        context=exec_context(AGENT_ID).describe(),
        timeout_s=60.0,
        state=None,
        incarnation=None,
    )
    env = child_env(tmp_path, request_path, result_path)
    # meiva:note
    # The host's end of stdin stays open until the child exits: end-of-file tells it to kill its group.
    proc = subprocess.Popen(
        [sys.executable, "-I", "-X", "utf8", "-m", "agent.execution.child"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    assert proc.stdout is not None
    deadline = time.monotonic() + 30
    out = ""
    while "before sleep" not in out:
        if time.monotonic() > deadline:
            proc.kill()
            pytest.fail(f"child never reached the sleep; output so far: {out!r}")
        out += proc.stdout.readline()
    os.kill(proc.pid, signal.SIGTERM)
    out += proc.stdout.read()
    assert proc.wait(timeout=60) == 0
    assert "before sleep" in out
    payload = read_result(result_path)
    assert payload.kind == "timed_out"


def test_child_turns_a_sigterm_during_boot_into_a_crash_envelope(tmp_path: Path) -> None:
    exec_dir = tmp_path / "exec"
    request_path = make_request_path(exec_dir, agent_id=AGENT_ID)
    result_path = make_result_path(exec_dir, agent_id=AGENT_ID)
    os.mkfifo(request_path)
    # meiva:note
    # The request file is a pipe nobody writes to, so the child blocks reading its request.
    # Opening the write end without blocking succeeds only once the child holds the read end,
    # which proves it is past boot and waiting there.
    proc = subprocess.Popen(
        [sys.executable, "-I", "-X", "utf8", "-m", "agent.execution.child"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=child_env(tmp_path, request_path, result_path),
    )
    writer = -1
    deadline = time.monotonic() + 60
    try:
        while writer < 0:
            try:
                writer = os.open(request_path, os.O_WRONLY | os.O_NONBLOCK)
            except OSError:
                assert proc.poll() is None, "the child exited before reading its request"
                assert time.monotonic() < deadline, "the child never reached its request"
                time.sleep(0.05)
        os.kill(proc.pid, signal.SIGTERM)
        assert proc.wait(timeout=60) == 0
    finally:
        if writer >= 0:
            os.close(writer)
        if proc.poll() is None:
            proc.kill()
        if proc.stdin is not None:
            proc.stdin.close()
        if proc.stdout is not None:
            proc.stdout.close()

    payload = read_result(result_path)
    assert payload.kind == "crashed"
    assert payload.exc_type == "TimeoutError"
    assert payload.code_reached is False
