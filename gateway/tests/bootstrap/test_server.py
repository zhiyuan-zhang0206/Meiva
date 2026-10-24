# meiva:note
# The gateway's process root, `gateway.cluster.server.main`.
#
# What the root decides is `gateway.cluster.serve_plan` (tested by value in
# `gateway/cluster/tests/test_serve_plan.py`) and the uvicorn parameters `serve_kwargs` assembles
# (asserted here by value). The root itself, which executes the plan, runs for real: a child
# process boots the production `main` against the throwaway Postgres with a one-route probe app,
# and the parent observes what a deployed gateway leaves behind: the pidfile, the served port and
# the addresses it listens on, the cluster's minimum code version, the child's log and the exit on
# SIGTERM. The order of the root's steps is observed through real faults: a renamed
# `schema_migrations` table, a trigger that refuses the minimum-version update, a database URL
# nothing listens on. Signal handlers, the fd limit and logging initialization are process
# globals; they run in the child, never in the test process.

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Generator, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import IO
from urllib.parse import urlsplit

import psutil
import psycopg
import pytest

from base.config.tests.configuration import snapshot_process_config
from base.native_process.code_version import CodeVersion
from gateway.cluster.process_boot import LOADED_IMAGE
from gateway.cluster.server import serve_kwargs
from gateway.tests.bootstrap.child_gateway_scenarios import BUILT_ENV, PORT_ENV
from gateway.tests.support.db_faults.missing_table import missing_table
from gateway.tests.support.db_faults.update_fault import failing_update

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CHILD_ENTRY = Path(__file__).with_name("child_gateway_scenarios.py")
# meiva:note
# isolated test credential.
_SECRET = "server-root-test-secret"  # noqa: S105
# meiva:note
# The address an every-interface IPv4 listener reports for its socket.
_IPV4_WILDCARD = str(ipaddress.IPv4Address(0))


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
    built_marker: Path

    @property
    def pidfile(self) -> Path:
        return self.home / "run" / "gateway.pid"

    def log(self) -> str:
        return self.log_path.read_text(encoding="utf-8", errors="replace")

    # meiva:note
    # The status and body a GET of `path` gets from the child, or None while nothing accepts
    # connections on its port.
    def get(self, path: str) -> tuple[int, bytes] | None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            return response.status, response.read()
        except OSError:
            return None
        finally:
            conn.close()

    # meiva:note
    # The probe app's answer, or None while nothing accepts connections yet.
    def probe(self) -> dict[str, int] | None:
        answer = self.get("/probe")
        return json.loads(answer[1]) if answer is not None and answer[0] == 200 else None

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

    # meiva:note
    # Wait until the child answers `path` with 200, and return the body.
    def wait_ok(self, path: str, *, timeout_s: float) -> bytes:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            answer = self.get(path)
            if answer is not None and answer[0] == 200:
                return answer[1]
            if self.proc.poll() is not None:
                pytest.fail(f"gateway root exited rc={self.proc.returncode}:\n{self.log()[-4000:]}")
            time.sleep(0.2)
        pytest.fail(f"gateway root never answered {path}:\n{self.log()[-4000:]}")

    # meiva:note
    # The addresses the child's listening sockets on the served port are bound to.
    def listening_addresses(self) -> set[str]:
        return {
            connection.laddr.ip
            for connection in psutil.Process(self.proc.pid).net_connections(kind="inet")
            if connection.status == psutil.CONN_LISTEN and connection.laddr.port == self.port
        }

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
    """Boot `main` in a child from a throwaway home whose `.env` declares the unit.

    `settings` are `MEIVA_*` values that replace the unit's defaults in the `.env` and in the
    child's environment alike, so they win whatever this test process was started with.
    """
    started: list[_Root] = []
    logs: list[IO[bytes]] = []

    def start(
        *, serves_gateway: bool, secret: str, settings: Mapping[str, str] | None = None
    ) -> _Root:
        home = tmp_path / f"home-{len(started)}"
        home.mkdir()
        port = distinct_free_ports(1)[0]
        values = {
            "MEIVA_DB_URL": db_url,
            "MEIVA_REDIS_URL": redis_url,
            "MEIVA_CLUSTER_SECRET": secret,
            "MEIVA_GATEWAY_PORT": str(port),
            "MEIVA_MACHINE_NAME": "root-test",
            "MEIVA_TIMEZONE": "UTC",
            "MEIVA_MACHINE_SERVE_GATEWAY": str(serves_gateway).lower(),
            "MEIVA_MACHINE_SERVE_AGENT_RUNNER": str(not serves_gateway).lower(),
            "MEIVA_GATEWAY_RELOAD": "0",
            **(settings or {}),
        }
        (home / ".env").write_text(
            "".join(f"{name}={value}\n" for name, value in values.items()), encoding="utf-8"
        )
        log_path = tmp_path / f"root-{len(started)}.log"
        log_file = log_path.open("wb")
        logs.append(log_file)
        built_marker = tmp_path / f"built-{len(started)}"
        env = os.environ.copy()
        env.update(values)
        env["MEIVA_HOME"] = str(home)
        env[PORT_ENV] = str(port)
        env[BUILT_ENV] = str(built_marker)
        # meiva:note
        # fixed interpreter + in-repo entry, no shell. The child leads its own process group,
        # so teardown reaches the hot-reload worker the child spawns as well.
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, str(_CHILD_ENTRY), "server_main"],
            cwd=_REPO_ROOT,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        root = _Root(proc=proc, home=home, port=port, log_path=log_path, built_marker=built_marker)
        started.append(root)
        return root

    yield start
    for root in started:
        with suppress(ProcessLookupError):
            os.killpg(root.proc.pid, signal.SIGKILL)
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


# meiva:note
# The database URL with its port replaced by one nothing listens on.
def _unreachable(db_url: str, distinct_free_ports: Callable[[int], list[int]]) -> str:
    parts = urlsplit(db_url)
    authority = parts.netloc.rpartition(":")[0]
    return parts._replace(netloc=f"{authority}:{distinct_free_ports(1)[0]}").geturl()


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
    assert root.built_marker.read_text() == "built"
    assert root.listening_addresses() == {"127.0.0.1"}
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


# meiva:note
# The secret is refused before the database is dialed. The same unit given a secret and a
# database nothing listens on fails dialing it, so the refusal that names the secret, with no
# connection error beside it, can only come from a check that ran first.
def test_a_missing_cluster_secret_is_refused_before_the_database_is_dialed(
    start_root: Callable[..., _Root], db_url: str, distinct_free_ports: Callable[[int], list[int]]
) -> None:
    unreachable = {"MEIVA_DB_URL": _unreachable(db_url, distinct_free_ports)}
    dials = start_root(serves_gateway=True, secret=_SECRET, settings=unreachable)
    refused = start_root(serves_gateway=True, secret="", settings=unreachable)

    assert dials.wait_exit() != 0
    assert "OperationalError" in dials.log()
    assert refused.wait_exit() != 0
    assert "MEIVA_CLUSTER_SECRET" in refused.log()
    assert "OperationalError" not in refused.log()
    assert not refused.pidfile.exists()


# meiva:note
# A database whose schema is not current stops the root before the logger starts, the pidfile
# is written or the minimum code version is raised.
def test_the_schema_is_asserted_before_logging_starts(
    start_root: Callable[..., _Root],
    db_conn: psycopg.Connection,
    minimum_code_version: Callable[[], int],
) -> None:
    before = minimum_code_version()
    with missing_table(db_conn, "schema_migrations"):
        root = start_root(serves_gateway=True, secret=_SECRET)
        assert root.wait_exit() != 0

    log = root.log()
    assert "Schema behind code" in log
    assert "service started" not in log
    assert not root.pidfile.exists()
    assert root.probe() is None
    assert minimum_code_version() == before


# meiva:note
# The minimum code version is raised after the logger started, so a refusal of the raise is
# logged: the database refuses the update, and the child's log holds the start-up line of the
# gateway process before the refusal.
def test_the_minimum_code_version_is_raised_after_logging_started(
    start_root: Callable[..., _Root],
    db_conn: psycopg.Connection,
    minimum_code_version: Callable[[], int],
) -> None:
    before = minimum_code_version()
    refusal = "minimum code version refused by the test"
    with failing_update(db_conn, "deployment_state", message=refusal):
        root = start_root(serves_gateway=True, secret=_SECRET)
        assert root.wait_exit() != 0

    log = root.log()
    assert "service started: gateway" in log
    assert log.index("service started: gateway") < log.index(refusal)
    assert root.probe() is None
    assert minimum_code_version() == before


# meiva:note
# A gateway that serves off the box binds every interface, and only under a declared
# transport-encryption posture: without the declaration the root refuses before it serves.
def test_an_off_box_gateway_without_a_transport_declaration_refuses_to_start(
    start_root: Callable[..., _Root], minimum_code_version: Callable[[], int]
) -> None:
    del minimum_code_version
    root = start_root(
        serves_gateway=True,
        secret=_SECRET,
        settings={"MEIVA_MACHINE_HOST": "10.0.0.5", "MEIVA_TRANSPORT_ENCRYPTION": ""},
    )

    assert root.wait_exit() != 0
    assert "MEIVA_TRANSPORT_ENCRYPTION" in root.log()
    assert root.probe() is None


def test_an_off_box_gateway_with_a_transport_declaration_binds_every_interface(
    start_root: Callable[..., _Root], minimum_code_version: Callable[[], int]
) -> None:
    del minimum_code_version
    root = start_root(
        serves_gateway=True,
        secret=_SECRET,
        settings={"MEIVA_MACHINE_HOST": "10.0.0.5", "MEIVA_TRANSPORT_ENCRYPTION": "overlay"},
    )

    root.wait_served()

    assert _IPV4_WILDCARD in root.listening_addresses()
    assert "127.0.0.1" not in root.listening_addresses()
    root.proc.send_signal(signal.SIGTERM)
    assert root.wait_exit() in (0, -signal.SIGTERM)


# meiva:note
# With hot reload the root hands uvicorn the factory path and the worker builds the real
# gateway app from the unit's environment; the parent never builds the app it would serve (its
# builder, which leaves the marker, is not called).
def test_with_hot_reload_the_parent_never_builds_the_app_a_worker_serves(
    start_root: Callable[..., _Root], minimum_code_version: Callable[[], int]
) -> None:
    del minimum_code_version
    root = start_root(serves_gateway=True, secret=_SECRET, settings={"MEIVA_GATEWAY_RELOAD": "1"})

    health = json.loads(root.wait_ok("/api/health", timeout_s=120))

    assert health["sha"] == LOADED_IMAGE.sha
    assert root.probe() is None
    assert not root.built_marker.exists()
    root.proc.send_signal(signal.SIGTERM)
    assert root.wait_exit(timeout_s=60) in (0, -signal.SIGTERM)
    assert root.get("/api/health") is None
