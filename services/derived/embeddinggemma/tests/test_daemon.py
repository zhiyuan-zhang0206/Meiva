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
from fastapi.testclient import TestClient

from base.host.env.registry import child_env
from services.derived.embeddinggemma import daemon
from services.derived.embeddinggemma.app import create_app
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


def test_a_server_told_to_require_a_key_exits_when_none_is_found(tmp_path: Path) -> None:
    config = fakes.server_config(tmp_path, require_api_key=True)
    with pytest.raises(RuntimeError, match="API key"):
        daemon.serve(config, open_engine=fakes.opener(fakes.FakeEngine()), api_key=None)


def test_the_key_comes_from_the_home_env_file_when_the_unit_environment_lacks_it(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text("MEIVA_OTHER='x'\nMEIVA_EMBEDDINGGEMMA_API_KEY='from-dotenv'\n")
    assert daemon.load_api_key({"MEIVA_HOME": str(tmp_path)}) == "from-dotenv"


def test_a_key_in_the_process_environment_wins_over_the_env_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("MEIVA_EMBEDDINGGEMMA_API_KEY=from-dotenv\n")
    environ = {"MEIVA_HOME": str(tmp_path), "MEIVA_EMBEDDINGGEMMA_API_KEY": "from-environ"}
    assert daemon.load_api_key(environ) == "from-environ"


def test_no_key_anywhere_is_none(tmp_path: Path) -> None:
    assert daemon.load_api_key({"MEIVA_HOME": str(tmp_path)}) is None
    (tmp_path / ".env").write_text("MEIVA_EMBEDDINGGEMMA_API_KEY=\n")
    assert daemon.load_api_key({"MEIVA_HOME": str(tmp_path)}) is None


def test_a_key_in_the_env_file_authenticates_a_served_request(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text('export MEIVA_EMBEDDINGGEMMA_API_KEY="file-secret"\n')
    key = daemon.load_api_key({"MEIVA_HOME": str(tmp_path)})
    config = fakes.server_config(tmp_path, require_api_key=True)
    app = create_app(config, open_engine=fakes.opener(fakes.FakeEngine()), api_key=key)
    body = {"model": fakes.MODEL, "input": ["x"], "dimensions": 768}
    with TestClient(app) as client:
        assert client.post("/v1/embeddings", json=body).status_code == 401
        granted = client.post(
            "/v1/embeddings", json=body, headers={"Authorization": "Bearer file-secret"}
        )
        assert granted.status_code == 200


def test_the_key_survives_the_environment_root_really_hands_a_managed_unit(
    tmp_path: Path,
) -> None:
    name = "MEIVA_EMBEDDINGGEMMA_API_KEY"
    (tmp_path / ".env").write_text(f"{name}='dotenv-secret'\n")
    forwarded = child_env({"MEIVA_HOME": str(tmp_path), name: "ambient-secret", "PATH": "/usr/bin"})
    assert name not in forwarded
    assert daemon.load_api_key(forwarded) == "dotenv-secret"
