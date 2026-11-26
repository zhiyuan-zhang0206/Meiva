from __future__ import annotations

import json
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from services.derived.embeddinggemma import daemon
from services.derived.embeddinggemma.tests import fakes

_CHILD = Path(__file__).with_name("serve_child.py")
_REPO = Path(__file__).resolve().parents[4]
_READY_DEADLINE_S = 30.0


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _health(port: int) -> dict[str, object] | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1.0) as answer:
            body: dict[str, object] = json.loads(answer.read())
            return body
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        return None


def test_sigterm_closes_the_engine_before_the_process_exits(tmp_path: Path) -> None:
    port = _free_port()
    child = subprocess.Popen(  # noqa: S603 - the interpreter and the repository's own child script
        [sys.executable, str(_CHILD), str(tmp_path), str(port)],
        cwd=_REPO,
        env={"PYTHONPATH": str(_REPO), "PATH": "/usr/bin:/bin"},
    )
    try:
        deadline = time.monotonic() + _READY_DEADLINE_S
        while _health(port) is None:
            assert child.poll() is None, "the served child exited before answering /healthz"
            assert time.monotonic() < deadline, "the served child never answered /healthz"
            time.sleep(0.1)
        child.send_signal(signal.SIGTERM)
        assert child.wait(timeout=_READY_DEADLINE_S) in {0, -signal.SIGTERM}
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
    assert (tmp_path / "closed.marker").read_text() == "closed"


def test_a_server_told_to_require_a_key_exits_when_the_environment_has_none(
    tmp_path: Path,
) -> None:
    config = fakes.server_config(tmp_path, require_api_key=True)
    with pytest.raises(RuntimeError, match="API key"):
        daemon.serve(config, open_engine=fakes.opener(fakes.FakeEngine()), environ={})
