# meiva:note
# The gateway's process root, `gateway.cluster.server.main`.
#
# What the root decides is `gateway.cluster.serve_plan` (tested by value in
# `gateway/cluster/tests/test_serve_plan.py`) and the uvicorn parameters `serve_kwargs` assembles
# (asserted here by value). The root itself, which executes the plan, runs for real: a child
# process boots the production `main` against the throwaway Postgres with a one-route probe app,
# and the parent observes what a deployed gateway leaves behind: the pidfile, the served port,
# the cluster's minimum code version and the exit on SIGTERM. Signal handlers, the fd limit and
# logging initialization are process globals; they run in the child, never in the test process.

from __future__ import annotations

import http.client
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import IO

import psycopg
import pytest

from base.config.tests.configuration import snapshot_process_config
from base.native_process.code_version import CodeVersion
from gateway.cluster.process_boot import LOADED_IMAGE
from gateway.cluster.server import serve_kwargs
from gateway.tests.bootstrap.child_gateway_scenarios import PORT_ENV

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CHILD_ENTRY = Path(__file__).with_name("child_gateway_scenarios.py")
# meiva:note
# isolated test credential.
_SECRET = "server-root-test-secret"  # noqa: S105


def test_serve_kwargs_pin_one_worker_and_the_drain_budget() -> None:
    config = snapshot_process_config()
    config.set_field("gateway_graceful_shutdown_timeout_seconds", 7.5)
    config.set_field("gateway_port", 18123)
    config.set_field("gateway_reload", False)
    kwargs = serve_kwargs(host="127.0.0.1", app="module:app", factory=False, config=config)
    # meiva:note
    # the rate limiters are process-local
    assert kwargs["workers"] == 1
    assert kwargs["timeout_graceful_shutdown"] == 7.5
    assert kwargs["port"] == 18123
    assert kwargs["log_config"] is None
    assert (kwargs["reload"], kwargs["reload_dirs"]) == (False, None)


def test_serve_kwargs_reload_watches_the_source_directories() -> None:
    config = snapshot_process_config()
    config.set_field("gateway_reload", True)
    kwargs = serve_kwargs(
        host="127.0.0.1",
        app="gateway.app:create_app_from_environment",
        factory=True,
        config=config,
    )
    assert kwargs["app"] == "gateway.app:create_app_from_environment"
    assert kwargs["factory"] is True
    assert kwargs["reload"] is True
    assert kwargs["reload_dirs"] == ["gateway", "base", "meiva", "agent"]
    assert kwargs["workers"] == 1


# meiva:note
# A child process running the production `main`, with the home it booted from.
@dataclass
class _Root:
    proc: subprocess.Popen[bytes]
    home: Path
    port: int
    log_path: Path

    @property
    def pidfile(self) -> Path:
        return self.home / "run" / "gateway.pid"

    def log(self) -> str:
        return self.log_path.read_text(encoding="utf-8", errors="replace")

    # meiva:note
    # The probe app's answer, or None while nothing accepts connections yet.
    def probe(self) -> dict[str, int] | None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", "/probe")
            response = conn.getresponse()
            return json.loads(response.read()) if response.status == 200 else None
        except OSError:
            return None
        finally:
            conn.close()

    def wait_served(self, *, timeout_s: float = 60.0) -> dict[str, int]:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            served = self.probe()
            if served is not None:
                return served
            if self.proc.poll() is not None:
                pytest.fail(f"gateway root exited rc={self.proc.returncode}:\n{self.log()[-4000:]}")
            time.sleep(0.1)
        pytest.fail(f"gateway root never served /probe:\n{self.log()[-4000:]}")

    def wait_exit(self, *, timeout_s: float = 30.0) -> int:
        try:
            return self.proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            pytest.fail(f"gateway root did not exit in {timeout_s:.0f}s:\n{self.log()[-4000:]}")


@pytest.fixture
def start_root(
    tmp_path: Path,
    db_url: str,
    redis_url: str,
    distinct_free_ports: Callable[[int], list[int]],
) -> Generator[Callable[..., _Root]]:
    """Boot `main` in a child from a throwaway home whose `.env` declares the unit."""
    started: list[_Root] = []
    logs: list[IO[bytes]] = []

    def start(*, serves_gateway: bool, secret: str) -> _Root:
        home = tmp_path / f"home-{len(started)}"
        home.mkdir()
        port = distinct_free_ports(1)[0]
        (home / ".env").write_text(
            "\n".join(
                [
                    f"MEIVA_DB_URL={db_url}",
                    f"MEIVA_REDIS_URL={redis_url}",
                    f"MEIVA_CLUSTER_SECRET={secret}",
                    f"MEIVA_GATEWAY_PORT={port}",
                    "MEIVA_MACHINE_NAME=root-test",
                    "MEIVA_TIMEZONE=UTC",
                    f"MEIVA_MACHINE_SERVE_GATEWAY={str(serves_gateway).lower()}",
                    f"MEIVA_MACHINE_SERVE_AGENT_RUNNER={str(not serves_gateway).lower()}",
                    "MEIVA_GATEWAY_RELOAD=0",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        log_path = tmp_path / f"root-{len(started)}.log"
        log_file = log_path.open("wb")
        logs.append(log_file)
        env = os.environ.copy()
        env["MEIVA_HOME"] = str(home)
        env[PORT_ENV] = str(port)
        # meiva:note
        # fixed interpreter + in-repo entry, no shell.
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, str(_CHILD_ENTRY), "server_main"],
            cwd=_REPO_ROOT,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        root = _Root(proc=proc, home=home, port=port, log_path=log_path)
        started.append(root)
        return root

    yield start
    for root in started:
        if root.proc.poll() is None:
            root.proc.kill()
        root.proc.wait(timeout=10)
    for log_file in logs:
        log_file.close()


@pytest.fixture
def minimum_code_version(db_conn: psycopg.Connection) -> Generator[Callable[[], int]]:
    """Read the cluster's minimum code version; restore what the test found."""

    def read() -> int:
        row = db_conn.execute("SELECT min_code_version FROM deployment_state WHERE id = 1")
        value = row.fetchone()
        assert value is not None
        db_conn.commit()
        return int(value[0])

    before = read()
    yield read
    db_conn.execute("UPDATE deployment_state SET min_code_version = %s WHERE id = 1", (before,))
    db_conn.commit()


def test_a_gateway_unit_serves_and_raises_the_minimum_code_version(
    start_root: Callable[..., _Root], minimum_code_version: Callable[[], int]
) -> None:
    assert minimum_code_version() < CodeVersion(LOADED_IMAGE).get()
    root = start_root(serves_gateway=True, secret=_SECRET)

    served = root.wait_served()

    assert served["pid"] == root.proc.pid
    assert root.pidfile.read_text() == str(root.proc.pid)
    assert minimum_code_version() == CodeVersion(LOADED_IMAGE).get()
    assert "rate limiters are process-local" in root.log()
    root.proc.send_signal(signal.SIGTERM)
    assert root.wait_exit() in (0, -signal.SIGTERM)


def test_a_runners_local_gateway_serves_without_touching_the_minimum(
    start_root: Callable[..., _Root], minimum_code_version: Callable[[], int]
) -> None:
    before = minimum_code_version()
    root = start_root(serves_gateway=False, secret="")

    root.wait_served()

    assert minimum_code_version() == before
    root.proc.send_signal(signal.SIGTERM)
    assert root.wait_exit() in (0, -signal.SIGTERM)


def test_a_gateway_without_a_cluster_secret_refuses_to_start(
    start_root: Callable[..., _Root], minimum_code_version: Callable[[], int]
) -> None:
    before = minimum_code_version()
    root = start_root(serves_gateway=True, secret="")

    assert root.wait_exit() != 0
    assert "MEIVA_CLUSTER_SECRET" in root.log()
    assert not root.pidfile.exists()
    assert root.probe() is None
    assert minimum_code_version() == before
