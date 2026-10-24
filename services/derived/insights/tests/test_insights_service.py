from __future__ import annotations

import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import httpx
import psycopg
import pytest
import uvicorn
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage
from psycopg_pool import ConnectionPool

from base.agents.messages.timestamp_policy import TimestampPolicy
from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from base.config import ConfigBoot
from base.db import Database
from base.host.env import port_lease
from base.lm.catalog import ModelCatalog
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.paths import insights_pidfile, insights_socket, meiva_home
from services.derived.insights import daemon
from services.derived.insights.app import build_app
from services.derived.insights.config import InsightsConfig
from services.derived.insights.daemon import bind_socket
from services.derived.insights.run_timeline.tests.test_projection import World
from services.derived.insights.run_timeline.tests.test_projection import world as world

_TIMELINE_INPUTS = TimestampPolicy(
    clock=Clock(ClockConfig("UTC", "UTC", False), timers=SYSTEM_TIMERS), timestamps_enabled=False
)
_CONFIG = InsightsConfig(
    run_timeline_message_text_max=20,
    default_model="claude-opus-5",
    timeline_inputs=_TIMELINE_INPUTS,
)
_REPO_ROOT = Path(__file__).resolve().parents[4]
_OTLP_ENDPOINT = "MEIVA_TELEMETRY_OTLP_ENDPOINT"


def _app(
    *,
    home: Path,
    model_catalog: ModelCatalog,
    timeline_inputs: TimestampPolicy = _TIMELINE_INPUTS,
    world: World | None = None,
) -> Any:
    app = build_app(
        world.db if world else cast(Database, object()),
        world.pool if world else cast(ConnectionPool[Any], object()),
        replace(_CONFIG, timeline_inputs=timeline_inputs),
        home=home,
        catalog=model_catalog,
    )
    if world:
        app.state.run_timeline_reader.read_segment = world.read_segment
    return app


def test_the_app_serves_the_run_timeline_routes_at_their_public_paths(
    tmp_path: Path, *, model_catalog: ModelCatalog
) -> None:
    paths = set(_app(home=tmp_path, model_catalog=model_catalog).openapi()["paths"])
    assert {
        "/api/agents/{agent_id}/run-timeline",
        "/api/agents/{agent_id}/run-timeline/messages",
        "/api/agents/{agent_id}/run-timeline/context",
    } <= paths


def test_healthz_names_the_service_home_and_process(
    tmp_path: Path, *, model_catalog: ModelCatalog
) -> None:
    body = TestClient(_app(home=tmp_path, model_catalog=model_catalog)).get("/healthz").json()
    assert body["name"] == "insights"
    assert isinstance(body["pid"], int)
    assert body["home"] == str(tmp_path)


def test_a_route_validates_before_it_reads_anything(
    tmp_path: Path, *, model_catalog: ModelCatalog
) -> None:
    client = TestClient(_app(home=tmp_path, model_catalog=model_catalog))

    assert client.get("/api/agents/1/run-timeline/messages?start=3&end=1").status_code == 422
    assert client.get("/api/agents/1/run-timeline/messages?start=-1&end=1").status_code == 422
    assert client.get("/api/agents/1/nowhere").status_code == 404


@pytest.fixture
def short_dir() -> Iterator[Path]:

    with tempfile.TemporaryDirectory(dir="/tmp") as name:
        yield Path(name)


def test_the_socket_is_owner_only_and_replaces_a_predecessors_leftover(short_dir: Path) -> None:
    path = short_dir / "insights.sock"
    path.write_text("left behind by a crashed predecessor")
    sock = bind_socket(path)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        assert stat.S_ISSOCK(path.stat().st_mode)
        assert mode == 0o600
    finally:
        sock.close()


def test_the_binder_creates_the_directory_of_its_socket(short_dir: Path) -> None:
    path = insights_socket(short_dir)
    assert not path.parent.exists()
    sock = bind_socket(path)
    try:
        assert path.parent == short_dir / "run" and path.exists()
    finally:
        sock.close()


def test_the_app_answers_over_the_bound_socket(
    short_dir: Path, *, model_catalog: ModelCatalog
) -> None:
    path = short_dir / "insights.sock"
    sock: socket.socket = bind_socket(path)
    server = uvicorn.Server(
        uvicorn.Config(
            _app(home=short_dir, model_catalog=model_catalog),
            log_level="warning",
            access_log=False,
            log_config=None,
        )
    )
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        transport = httpx.HTTPTransport(uds=str(path), retries=20)
        with httpx.Client(transport=transport, base_url="http://insights") as client:
            reply = client.get("/api/agents/1/run-timeline/messages?start=3&end=1")
        assert reply.status_code == 422
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
    assert not thread.is_alive()


def _policy(*, enabled: bool) -> TimestampPolicy:
    return TimestampPolicy(
        clock=Clock(
            ClockConfig("UTC", "UTC", False),
            now=lambda: datetime(2026, 10, 10, 12, tzinfo=UTC),
            timers=SYSTEM_TIMERS,
        ),
        timestamps_enabled=enabled,
    )


def _compact_history(world: World) -> list[str]:
    world.put(
        [
            HumanMessage(
                content="summary",
                additional_kwargs={
                    "meiva_msg_type": "compact_summary",
                    "meiva_created_at": "2026-10-10T12:00:00+00:00",
                },
            )
        ],
        sealed=False,
        agent=9,
    )
    return world.loads


def test_apps_keep_their_rendering_policy_and_cache_separate(
    world: World, tmp_path: Path, *, model_catalog: ModelCatalog
) -> None:
    loads = _compact_history(world)
    hidden, shown = _policy(enabled=False), _policy(enabled=True)
    first = _app(home=tmp_path, model_catalog=model_catalog, timeline_inputs=hidden, world=world)
    second = _app(home=tmp_path, model_catalog=model_catalog, timeline_inputs=shown, world=world)
    assert first.state.insights_config.timeline_inputs is hidden
    assert second.state.insights_config.timeline_inputs is shown
    path = "/api/agents/9/run-timeline/messages?start=0&end=0&full=true"

    assert TestClient(first).get(path).json()["messages"][0]["parts"][0]["text"] == (
        "Compact summary:\n\nsummary"
    )
    assert TestClient(second).get(path).json()["messages"][0]["parts"][0]["text"] == (
        "Compact summary [2026-10-10 12:00:00]:\n\nsummary"
    )
    assert len(loads) == 2


@pytest.fixture
def owned_config_environment() -> Iterator[None]:
    try:
        with patch.dict(os.environ):
            yield
    finally:
        time.tzset()


# meiva:note
# The root reads the process configuration once into the frozen value the app is built from: the
# text limit and the default model as plain values, the clock (in the cluster's zone) and the
# timestamp switch as one `TimestampPolicy`. A later edit of the owner does not reach it.
@pytest.mark.usefixtures("owned_config_environment")
def test_the_root_builds_one_config_from_the_process_configuration() -> None:
    boot = ConfigBoot()
    boot.set_field("timezone", "Asia/Tokyo")
    boot.set_field("message_timestamp_weekday", False)
    boot.set_field("message_timestamps", False)
    boot.set_field("run_timeline_message_text_max", 37)
    boot.set_field("llm_model", "claude-opus-5")

    config = daemon.config_from_boot(boot)
    boot.set_field("llm_model", "claude-haiku-4-5-20251001")

    assert config.run_timeline_message_text_max == 37
    assert config.default_model == "claude-opus-5"
    assert config.timeline_inputs.timestamps_enabled is False
    assert config.timeline_inputs.clock.format_timestamp(
        datetime(2026, 10, 10, 12, tzinfo=UTC)
    ) == ("[2026-10-10 21:00:00]")


# meiva:note
# One port this session holds, from the host-wide lease: the number is refused to
# every other live holder on the host, so two workers — or two runs of one shard on
# one machine — cannot be given it.
def _free_port() -> int:
    return port_lease.lease_ports(1)[0]


def _backend_names(db_conn: psycopg.Connection) -> set[str]:
    db_conn.rollback()
    rows = db_conn.execute(
        "SELECT DISTINCT application_name FROM pg_stat_activity WHERE application_name LIKE 'meiva:%%'"
    ).fetchall()
    return {row[0] for row in rows}


# meiva:note
# The entry's first `/healthz` answer over its socket; fails with the log tail when the process
# exits or 90 seconds pass first.
def _wait_for_health(
    http: httpx.Client, proc: subprocess.Popen[bytes], log_path: Path
) -> httpx.Response:
    deadline = time.monotonic() + 90.0
    while proc.poll() is None and time.monotonic() <= deadline:
        try:
            return http.get("/healthz")
        except httpx.TransportError:
            time.sleep(0.1)
    pytest.fail(f"the entry never served:\n{log_path.read_text()[-4000:]}")


# meiva:note
# Wait until the entry's database connection shows up under its application name.
def _wait_for_backend(
    db_conn: psycopg.Connection, name: str, proc: subprocess.Popen[bytes], log_path: Path
) -> None:
    deadline = time.monotonic() + 30.0
    while name not in _backend_names(db_conn):
        if proc.poll() is not None or time.monotonic() > deadline:
            pytest.fail(f"the entry never connected as {name!r}:\n{log_path.read_text()[-4000:]}")
        time.sleep(0.1)


# meiva:note
# The production entry as a real process: it captures its commit, checks the schema, builds its
# logging and database from one gate, serves the app on its owner-only socket, touches the
# database for a request, and leaves cleanly on SIGTERM, removing its pidfile and socket. The
# child gets a home of its own (short, because a unix socket path is short) and a dead telemetry
# endpoint; the database and Redis are the test's throwaway ones, inherited through the environment.
def test_the_entry_serves_over_its_socket_with_the_captured_commit_and_stops_on_sigterm(
    db_conn: psycopg.Connection, short_dir: Path
) -> None:
    home = short_dir / "home"
    home.mkdir()
    dead_telemetry = f"http://127.0.0.1:{_free_port()}"
    # meiva:note
    # The test cluster's own configuration (database, Redis, ports) plus this child's overrides.
    (home / ".env").write_text(
        (meiva_home() / ".env").read_text(encoding="utf-8")
        + f"\n{_OTLP_ENDPOINT}={dead_telemetry}\nMEIVA_MACHINE_NAME=insights-entry-test\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["MEIVA_HOME"] = str(home)
    env[_OTLP_ENDPOINT] = dead_telemetry
    env.pop("MEIVA_PERMISSIONS_HELPER_PID", None)
    log_path = short_dir / "insights.log"
    expected_name = f"meiva:insights:v{CodeVersion(LoadedCommit.capture()).get()}"
    socket_path = insights_socket(home)
    with log_path.open("wb") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "services.derived.insights.daemon"],
            cwd=_REPO_ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            transport = httpx.HTTPTransport(uds=str(socket_path))
            with httpx.Client(transport=transport, base_url="http://insights", timeout=5.0) as http:
                health = _wait_for_health(http, proc, log_path)
                assert health.status_code == 200
                body = health.json()
                assert body["name"] == "insights" and body["pid"] == proc.pid
                assert Path(body["home"]).resolve() == home.resolve()
                # meiva:note
                # A route that reads the database: the pool connects under the entry's gate.
                assert http.get(
                    "/api/agents/1/run-timeline/messages?start=0&end=0"
                ).status_code in (
                    404,
                    503,
                )
            _wait_for_backend(db_conn, expected_name, proc, log_path)
            assert insights_pidfile(home).exists() and stat.S_ISSOCK(socket_path.stat().st_mode)

            proc.send_signal(signal.SIGTERM)
            exit_code = proc.wait(timeout=30.0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5.0)

    assert exit_code == 0, log_path.read_text()[-4000:]
    assert "[insights] interrupted, shutting down" in log_path.read_text()
    assert not insights_pidfile(home).exists()
    assert not socket_path.exists()
