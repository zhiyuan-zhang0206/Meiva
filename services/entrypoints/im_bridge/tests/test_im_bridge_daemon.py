from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.cluster.machine import GatewayAccess, GatewayApiTokenMissing, gateway_auth_headers
from base.config import ConfigBoot
from base.daemon.health import Liveness, start_health_server
from base.db import Database
from base.native_process.loaded_commit import LoadedCommit
from services.entrypoints.im_bridge import daemon
from services.entrypoints.im_bridge.adapters.feishu import FeishuAdapter
from services.entrypoints.im_bridge.adapters.run_plan import (
    ADAPTER_NAMES,
    exit_code,
    plan_adapters,
)
from services.entrypoints.im_bridge.adapters.telegram import TelegramAdapter
from services.entrypoints.im_bridge.adapters.weixin import WeixinAdapter
from services.entrypoints.im_bridge.tests.slices import im_bridge_config
from services.entrypoints.im_bridge.tests.support import DurableAdapter, owned_tasks
from services.entrypoints.im_bridge.tests.test_im_bridge_core import (
    CoreFactory,
    FakeGateway,
)
from services.entrypoints.im_bridge.tests.test_im_bridge_core import make_core as make_core
from services.entrypoints.im_bridge.types import IMAdapter

_REPO_ROOT = Path(__file__).resolve().parents[4]
_TIMINGS = daemon.DaemonTimings(liveness_beat_s=0.02, notice_poll_s=0.05, outbound_round_s=0.05)


def _free_port() -> int:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return reservation.getsockname()[1]


def _get(port: int, path: str = "/healthz") -> tuple[int, dict[str, Any]] | None:
    try:
        # meiva:note
        # a loopback URL built from the daemon's own health port.
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, {}
    except OSError:
        return None


def _post(port: int, path: str, body: bytes = b"{}") -> int:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, method="POST")
    try:
        # meiva:note
        # a loopback URL built from the daemon's own health port.
        with urllib.request.urlopen(request, timeout=2) as response:  # noqa: S310
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


async def _until(predicate: Any, what: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await asyncio.to_thread(predicate):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def served_config(process_config: ConfigBoot) -> ConfigBoot:
    process_config.set_field("im_bridge_health_port", _free_port())
    process_config.set_field("im_disabled_adapters", list(ADAPTER_NAMES))
    return process_config


def _run(
    config: ConfigBoot, database: Database, tmp_path: Path, **overrides: Any
) -> asyncio.Task[None]:
    return asyncio.create_task(
        daemon.run(
            process_config=config,
            runtime=tmp_path / "run",
            database=lambda: database,
            image=LoadedCommit(Path(), None),
            timings=_TIMINGS,
            **overrides,
        )
    )


def test_liveness_loop_beats_periodically() -> None:
    liveness = Liveness(timeout_s=0.2)

    async def scenario() -> None:
        task = asyncio.create_task(daemon.liveness_loop(liveness, interval_s=0.02))
        try:
            await asyncio.sleep(0.5)
            assert liveness.is_alive()
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    asyncio.run(scenario())


async def test_run_serves_health_and_the_send_route_then_stops_cleanly(
    served_config: ConfigBoot, database: Database, tmp_path: Path
) -> None:
    port = served_config.get_field("im_bridge_health_port")
    task = _run(served_config, database, tmp_path)
    try:
        await _until(lambda: _get(port) is not None, "the health endpoint")
        status, health = cast(tuple[int, dict[str, Any]], await asyncio.to_thread(_get, port))
        assert status == 200 and health["name"] == "im_bridge"
        assert (tmp_path / "run" / "im_bridge.pid").exists()
        assert await asyncio.to_thread(_post, port, "/send") == 401, "the send route needs a bearer"
        await asyncio.sleep(0.2)
        assert (cast(tuple[int, dict[str, Any]], await asyncio.to_thread(_get, port)))[0] == 200
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert await asyncio.to_thread(_get, port) is None
    assert not (tmp_path / "run" / "im_bridge.pid").exists()


async def test_a_child_fault_fails_the_daemon_and_every_resource_is_cleaned_up(
    served_config: ConfigBoot,
    database: Database,
    db_conn: psycopg.Connection,
    tmp_path: Path,
) -> None:
    port = served_config.get_field("im_bridge_health_port")
    task = _run(served_config, database, tmp_path)
    try:
        await _until(lambda: _get(port) is not None, "the health endpoint")
        db_conn.commit()
        db_conn.execute("ALTER TABLE agent_notices RENAME TO agent_notices_hidden_by_test")
        db_conn.commit()
        with pytest.raises(ExceptionGroup) as caught:
            await asyncio.wait_for(task, timeout=15)
    finally:
        db_conn.rollback()
        db_conn.execute("ALTER TABLE agent_notices_hidden_by_test RENAME TO agent_notices")
        db_conn.commit()
        if not task.done():
            task.cancel()
    assert caught.group_contains(psycopg.errors.UndefinedTable)
    assert await asyncio.to_thread(_get, port) is None
    assert not (tmp_path / "run" / "im_bridge.pid").exists()


async def test_a_startup_fault_still_removes_the_pidfile_and_closes_the_pool(
    served_config: ConfigBoot, database: Database, tmp_path: Path
) -> None:
    served_config.set_field("cluster_secret", "")
    with pytest.raises(ExceptionGroup) as caught:
        await asyncio.wait_for(_run(served_config, database, tmp_path), timeout=15)
    assert caught.group_contains(GatewayApiTokenMissing)
    assert not (tmp_path / "run" / "im_bridge.pid").exists()


class _StopFault(DurableAdapter):
    channel = "stopfault"

    def __init__(self, fault: BaseException | None) -> None:
        super().__init__(core=None)
        self.fault = fault
        self.stopped = False

    async def start(self, tasks: asyncio.TaskGroup) -> None:
        return None

    async def stop(self) -> None:
        self.stopped = True
        if self.fault is not None:
            raise self.fault

    async def send(
        self,
        chat_id: str,
        text: str,
        *,
        buttons: list[tuple[str, str]] | None = None,
        markdown: bool = False,
    ) -> None:
        del chat_id, text, buttons, markdown


async def test_cleanup_attempts_every_resource_and_reports_an_unknown_adapter_fault(
    process_config: ConfigBoot, make_core: CoreFactory, pool: ConnectionPool, tmp_path: Path
) -> None:
    fault = RuntimeError("unknown adapter stop fault")
    broken, healthy = _StopFault(fault), _StopFault(None)
    health = await start_health_server(
        "im_bridge", 0, image=LoadedCommit(Path(), None), liveness=Liveness(5.0)
    )
    port = health.sockets[0].getsockname()[1]
    pidfile = tmp_path / "run" / "im_bridge.pid"
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text("1\n")
    async with owned_tasks() as tasks:
        core = make_core(FakeGateway(), tasks=tasks)
        adapters: list[IMAdapter] = [broken, healthy]
        errors = await asyncio.wait_for(
            daemon.stop_resources(
                core, adapters, health, pool, config=process_config, runtime=tmp_path / "run"
            ),
            timeout=5,
        )
    assert errors == [fault]
    assert broken.stopped and healthy.stopped
    assert await asyncio.to_thread(_get, port) is None
    assert pool.closed
    assert not pidfile.exists()


def test_the_plan_loads_the_enabled_adapters_in_order() -> None:
    plan = plan_adapters(frozenset({"weixin", "feishu"}))
    assert plan.load == ("telegram",)
    assert plan.disabled == ("weixin", "feishu")
    assert plan_adapters(frozenset()).load == ADAPTER_NAMES
    assert plan_adapters(frozenset({"retired-channel"})).load == ADAPTER_NAMES


@pytest.mark.parametrize(
    ("crashed", "interrupted", "drain_failures", "code"),
    [
        (False, False, 0, 0),
        (False, True, 0, 0),
        (False, True, 2, 1),
        (True, False, 0, 1),
        (True, True, 0, 1),
    ],
)
def test_the_exit_status_follows_how_the_run_ended(
    crashed: bool, interrupted: bool, drain_failures: int, code: int
) -> None:
    assert (
        exit_code(crashed=crashed, interrupted=interrupted, drain_failures=drain_failures) == code
    )


def test_load_adapters_builds_only_the_enabled_ones(
    process_config: ConfigBoot, make_core: CoreFactory, state_dir: Path
) -> None:
    core = make_core(FakeGateway())
    inputs = daemon.adapter_inputs(process_config, state_dir=state_dir)
    loaded = daemon.load_adapters(core, plan_adapters(frozenset({"weixin", "feishu"})), inputs)
    assert [type(adapter) for adapter in loaded] == [TelegramAdapter]
    assert core.adapters["telegram"] is loaded[0]


def test_load_adapters_hands_each_adapter_its_slice(
    process_config: ConfigBoot, make_core: CoreFactory, state_dir: Path
) -> None:
    core = make_core(FakeGateway())
    inputs = dataclasses.replace(
        daemon.adapter_inputs(process_config, state_dir=state_dir), proxy_configured=True
    )
    telegram, weixin, feishu = daemon.load_adapters(core, plan_adapters(frozenset()), inputs)
    assert isinstance(telegram, TelegramAdapter) and telegram._config == inputs.telegram
    assert isinstance(weixin, WeixinAdapter) and weixin._state_dir == state_dir
    assert isinstance(feishu, FeishuAdapter) and feishu._config == inputs.feishu
    assert feishu._proxy_configured is True and feishu._state_dir == state_dir


def test_invalid_adapter_configuration_is_not_skipped(
    process_config: ConfigBoot, make_core: CoreFactory, state_dir: Path
) -> None:
    state_dir.mkdir(parents=True)
    (state_dir / "weixin_account.json").write_text(
        json.dumps({"account_id": "a", "bot_token": "t", "base_url": "http://plain.example"})
    )
    core = make_core(FakeGateway())
    inputs = daemon.adapter_inputs(process_config, state_dir=state_dir)
    with pytest.raises(ValueError, match="qualified HTTPS namespace"):
        daemon.load_adapters(core, plan_adapters(frozenset({"telegram", "feishu"})), inputs)


@pytest.mark.parametrize("build", ["im_bridge_config", "telegram_config", "feishu_config"])
def test_a_built_slice_carries_the_live_value_of_every_field(
    build: str, process_config: ConfigBoot
) -> None:
    process_config.set_field("im_disabled_adapters", ["weixin"])
    process_config.set_field("im_send_retry_delays", [0.5, 1.5])
    process_config.set_field("telegram_owner_id", 4242)
    process_config.set_field("feishu_app_id", "cli_x")
    config = getattr(daemon, build)(process_config)
    for field in dataclasses.fields(config):
        flat: Any = process_config.get_field(field.name)
        expected = tuple(cast(list[Any], flat)) if isinstance(flat, list) else flat
        assert getattr(config, field.name) == expected


def test_the_gateway_client_gets_the_gateway_url_and_bearer_from_the_root() -> None:
    # meiva:note
    # A test credential for a fake account; the S106 suppression is the harness's.
    access = GatewayAccess(
        gateway_url="http://gateway.example:8000",
        cluster_secret="secret-for-the-test",  # noqa: S106
        remote_plane=True,
        machine_token=None,
        launcher_profile=None,
        gateway_home=False,
    )
    client = daemon.gateway_client(
        im_bridge_config(), gateway_url="http://gateway.example:8000/", access=access
    )
    assert client._base == "http://gateway.example:8000"
    assert client._headers() == gateway_auth_headers(access)


def test_httpx_info_logs_gated(caplog: pytest.LogCaptureFixture) -> None:
    httpx_logger = logging.getLogger("httpx")
    previous_level = httpx_logger.level
    caplog.set_level(logging.INFO)
    try:
        httpx_logger.setLevel(logging.NOTSET)
        caplog.clear()
        httpx_logger.info(
            "HTTP Request: GET https://api.telegram.org/bot1234567890:FAKE-TOKEN/getUpdates"
        )
        assert "FAKE-TOKEN" in caplog.text

        daemon.gate_httpx_info_logs()
        assert httpx_logger.getEffectiveLevel() == logging.WARNING
        caplog.clear()
        httpx_logger.info(
            "HTTP Request: GET https://api.telegram.org/bot1234567890:FAKE-TOKEN/getUpdates"
        )
        httpx_logger.warning("connection pool is full, discarding connection")

        assert "FAKE-TOKEN" not in caplog.text
        assert "connection pool is full" in caplog.text
    finally:
        httpx_logger.setLevel(previous_level)


@pytest.fixture
def entry_home(tmp_path: Path, db_url: str, redis_url: str) -> Iterator[Path]:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text(
        "\n".join(
            [
                f"MEIVA_DB_URL={db_url}",
                f"MEIVA_REDIS_URL={redis_url}",
                "MEIVA_MACHINE_NAME=im-entry-test",
                "MEIVA_CLUSTER_SECRET=im-entry-test-secret",
                "MEIVA_MACHINE_SERVE_GATEWAY=true",
                "MEIVA_TIMEZONE=UTC",
                "MEIVA_TELEMETRY_OTLP_ENABLED=false",
                f"MEIVA_IM_DISABLED_ADAPTERS={','.join(ADAPTER_NAMES)}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    yield home


def _start_entry(home: Path, port: int) -> subprocess.Popen[str]:
    env = {**os.environ, "MEIVA_HOME": str(home), "MEIVA_IM_BRIDGE_HEALTH_PORT": str(port)}
    # meiva:note
    # the interpreter running the tests and the module under test, no shell.
    return subprocess.Popen(
        [sys.executable, "-m", "services.entrypoints.im_bridge.daemon"],
        cwd=_REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def test_the_entry_serves_and_a_stop_signal_ends_it_with_status_zero(entry_home: Path) -> None:
    port = _free_port()
    child = _start_entry(entry_home, port)
    try:
        deadline = time.monotonic() + 90
        served = None
        while served is None and time.monotonic() < deadline:
            if child.poll() is not None:
                pytest.fail(f"the entry exited before serving:\n{child.communicate()[0][-3000:]}")
            served = _get(port)
            time.sleep(0.1)
        assert served is not None and served[1]["name"] == "im_bridge"
        child.send_signal(signal.SIGTERM)
        output, _ = child.communicate(timeout=60)
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate()
    assert child.returncode == 0, output
    assert _get(port) is None
    assert not (entry_home / "run" / "im_bridge.pid").exists()


def test_the_entry_exits_with_status_one_when_the_run_crashes(entry_home: Path) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        child = _start_entry(entry_home, taken.getsockname()[1])
        try:
            output, _ = child.communicate(timeout=90)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate()
    assert child.returncode == 1, output
    assert "daemon crashed" in output
