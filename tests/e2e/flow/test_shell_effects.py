from __future__ import annotations

import os
import shutil
import signal
import time
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import httpx
import psutil
import psycopg
import pytest

from base.config import settings
from base.paths import meiva_home
from base.sessions.backend import PtySessionBackend
from base.sessions.pty.client import PtyClientConfig
from base.sessions.pty.tests.pty_service import service_socket
from base.sessions.tests.poll_until import poll_until
from tests.e2e._credentials import gateway_get, gateway_post
from tests.e2e._db import chat_and_wait, wait_for_status
from tests.e2e._ports import GATEWAY_SOCKET, GATEWAY_URL
from tests.e2e.fakes.scenario_recording import model_inputs, reset_record
from tests.e2e.fakes.scenarios import shell_effects as world
from tests.e2e.process_support import managed_proc, registered_server, wait_for_port

Call = list[dict[str, Any]]


@pytest.fixture
def clean_shell_world() -> Iterator[None]:
    reset_record()
    shutil.rmtree(world.sandbox(), ignore_errors=True)
    world.sandbox().mkdir(parents=True)
    yield
    shutil.rmtree(world.sandbox(), ignore_errors=True)
    reset_record()


def _tool_text(call: Call) -> str:
    return "\n".join(m["text"] for m in call if m["type"] == "tool")


def _human_text(call: Call) -> str:
    return "\n".join(m["text"] for m in call if m["type"] == "human")


def _human_witness(agent_id: int, *markers: str) -> tuple[bool, object]:
    calls = model_inputs(agent_id)
    human = "\n".join(_human_text(call) for call in calls)
    missing = [marker for marker in markers if marker not in human]
    return not missing, {"missing": missing, "model_calls": len(calls)}


def _inbounds(agent_id: int, prefix: str) -> list[tuple[str, str, str]]:
    with psycopg.connect(settings.data_plane.db_url) as conn:
        rows = conn.execute(
            "SELECT source, content, status FROM inbound_messages "
            "WHERE agent_id = %s AND source LIKE %s ORDER BY id",
            (agent_id, prefix + "%"),
        ).fetchall()
    return [(str(source), str(content), str(status)) for source, content, status in rows]


@contextmanager
def _restarted_server(label: str, *, pass_gateway_fd: bool = False) -> Generator[dict[str, str]]:
    server = registered_server(label)
    old, log_path = server.process, server.log_path
    assert isinstance(old.args, list)
    env = psutil.Process(old.pid).environ()
    os.killpg(old.pid, signal.SIGTERM)
    old.wait(timeout=15.0)
    with managed_proc(
        cast(list[str], old.args),
        env=env,
        label=label,
        log_path=f"{log_path}.restart" if log_path is not None else None,
        pass_fds=(GATEWAY_SOCKET.fileno(),) if pass_gateway_fd else (),
    ) as replacement:
        assert replacement.poll() is None
        yield env


@pytest.mark.scenario("tests.e2e.fakes.scenarios.shell_effects:build_background")
def test_background_exit_wakes_agent_with_log_and_tail(
    spawned_agent: int, clean_shell_world: None
) -> None:
    chat_and_wait(spawned_agent, "start background job")
    sid_text, path, elapsed_text = (world.sandbox() / "background-handle").read_text().splitlines()
    sid = int(sid_text)
    assert float(elapsed_text) < 3.0, "run_background waited for the five-second command"
    assert f"background-started {sid}" in _tool_text(model_inputs(spawned_agent)[1])

    def completed() -> tuple[bool, object]:
        rows = _inbounds(spawned_agent, f"shell:{sid}")
        return bool(rows and rows[0][2] == "done"), rows

    poll_until(completed, timeout=60.0, interval=0.3, what="background completion delivered")
    source, content, _ = _inbounds(spawned_agent, f"shell:{sid}")[0]
    assert source == f"shell:{sid}"
    assert "finished" in content and "exited with code" not in content
    assert path in content and "BG-TAIL-MARK" in content
    assert "BG-TAIL-MARK" in Path(path).read_text()
    poll_until(
        lambda: _human_witness(spawned_agent, "BG-TAIL-MARK"),
        timeout=60.0,
        interval=0.3,
        what="background tail in model input",
    )


@pytest.mark.scenario("tests.e2e.fakes.scenarios.shell_effects:build_background_policy")
def test_background_completion_respects_the_hourly_policy(
    spawned_agent: int, clean_shell_world: None
) -> None:
    agent = spawned_agent
    response = gateway_post(
        f"{GATEWAY_URL}/api/agents/{agent}/restart",
        json={
            "message": "apply hourly policy",
            "config_overlay": {"completion_notice_policy": "hourly"},
        },
        timeout=30.0,
    )
    response.raise_for_status()
    chat_and_wait(agent, "start two jobs")
    first_id, first_path, second_id, second_path = (
        (world.sandbox() / "policy-handles").read_text().splitlines()
    )

    def buffered() -> tuple[bool, object]:
        with psycopg.connect(settings.data_plane.db_url) as conn:
            count = conn.execute(
                "SELECT count(*) FROM completion_notice_events WHERE agent_id = %s", (agent,)
            ).fetchone()
        return bool(count and count[0] == 2), count

    poll_until(buffered, timeout=60.0, interval=0.3, what="both completions buffered")
    assert "POLICY-SUCCESS" in Path(first_path).read_text()
    assert "POLICY-FAILURE" in Path(second_path).read_text()
    assert _inbounds(agent, f"shell:{first_id}") == []
    assert _inbounds(agent, f"shell:{second_id}") == []


@pytest.mark.scenario("tests.e2e.fakes.scenarios.shell_effects:build_session_verbs")
def test_session_verbs_run_in_one_shell_and_change_its_lifetime(
    spawned_agent: int, clean_shell_world: None
) -> None:
    chat_and_wait(spawned_agent, "exercise persistent session")
    sid = int((world.sandbox() / "session-id").read_text())
    assert (world.sandbox() / "sent").read_text().strip() == "SEND-MARK"
    assert (world.sandbox() / "keys").read_text().strip() == "KEY-MARK"
    tool = _tool_text(model_inputs(spawned_agent)[1])
    assert "capture-has-mark True" in tool, tool
    assert f"listed-before {{{sid}: 'e2e-verbs'}}" in tool, tool
    assert "renewed-until" in tool and "listed-after-kill {}" in tool, tool
    assert "kill-all-count 1" in tool and "listed-after-kill-all {}" in tool, tool
    with psycopg.connect(settings.data_plane.db_url) as conn:
        row = conn.execute(
            "SELECT renewals FROM agent_shell_ttls WHERE agent_id = %s AND session_id = %s",
            (spawned_agent, sid),
        ).fetchone()
    assert row == (1,)


@pytest.mark.scenario("tests.e2e.fakes.scenarios.shell_effects:build_survival")
def test_session_survives_agent_host_and_gateway_restarts(
    spawned_agent: int, clean_shell_world: None
) -> None:
    agent = spawned_agent
    chat_and_wait(agent, "create survivor session")
    sid = int((world.sandbox() / "survivor-id").read_text())
    full_name = f"meiva-agent-{agent}-shell-{sid}-e2e-survivor"
    backend = PtySessionBackend(
        config=PtyClientConfig(socket_path=service_socket()), home=meiva_home()
    )

    def usable(stage: str) -> None:
        assert backend.has_session(full_name), f"session disappeared after {stage}"
        marker = world.sandbox() / stage
        backend.send(full_name, f"echo {stage} > {marker}")
        backend.send_keys(full_name, "Enter")
        poll_until(
            lambda: (marker.exists(), str(marker)),
            timeout=10.0,
            interval=0.2,
            what=f"same session executes after {stage}",
        )

    usable("initial")
    restart = gateway_post(
        f"{GATEWAY_URL}/api/agents/{agent}/restart",
        json={"message": "restart shell effects"},
        timeout=30.0,
    )
    restart.raise_for_status()

    def applied() -> tuple[bool, object]:
        with psycopg.connect(settings.data_plane.db_url) as conn:
            row = conn.execute(
                "SELECT status FROM inbound_messages WHERE agent_id = %s "
                "AND kind = 'restart' ORDER BY id DESC LIMIT 1",
                (agent,),
            ).fetchone()
        return bool(row and row[0] == "done"), row

    poll_until(applied, timeout=30.0, interval=0.3, what="agent restart applied")
    usable("agent-restart")

    with _restarted_server("agent-host") as host_env:
        wait_for_port(
            "127.0.0.1", int(host_env["MEIVA_AGENT_HOST_HEALTH_PORT"]), label="restarted agent host"
        )
        usable("host-restart")
        with _restarted_server("gateway", pass_gateway_fd=True):

            def gateway_ready() -> tuple[bool, object]:
                try:
                    response = gateway_get(f"{GATEWAY_URL}/api/agents", timeout=2.0)
                except httpx.HTTPError as exc:
                    return False, repr(exc)
                return response.status_code == 200, response.status_code

            poll_until(
                gateway_ready,
                timeout=30.0,
                interval=0.3,
                what="restarted gateway answers",
            )
            usable("gateway-restart")
            chat_and_wait(agent, "use the survivor after all restarts")
            tool = _tool_text(model_inputs(agent)[-1])
            assert "survivor-listed e2e-survivor" in tool, tool
            poll_until(
                lambda: ((world.sandbox() / "after").exists(), "agent SDK sent to session"),
                timeout=10.0,
                interval=0.2,
                what="agent's SDK uses the surviving session",
            )


@pytest.mark.scenario("tests.e2e.fakes.scenarios.shell_effects:build_watchers")
def test_background_and_schedules_wake_owner_with_their_sources(
    spawned_agent: int, clean_shell_world: None
) -> None:
    chat_and_wait(spawned_agent, "start watchers")
    launch, at, cron = map(int, (world.sandbox() / "watcher-ids").read_text().split())

    def delivered() -> tuple[bool, object]:
        rows = _inbounds(spawned_agent, "shell:") + _inbounds(spawned_agent, "schedule:")
        sources = {source for source, _, _ in rows}
        return {f"shell:{launch}", f"schedule:{at}", f"schedule:{cron}"} <= sources, rows

    try:
        poll_until(delivered, timeout=115.0, interval=0.5, what="background, at and cron wakes")
    finally:
        # meiva:note
        # The recurring schedule outlives its first fire; cancel it before the
        # fixture removes the sandbox and avoid a second wake in later tests.
        gateway_post(f"{GATEWAY_URL}/api/schedules/{cron}/cancel", timeout=10.0).raise_for_status()
    rows = _inbounds(spawned_agent, "shell:") + _inbounds(spawned_agent, "schedule:")
    assert any(
        source == f"shell:{launch}" and "LAUNCH-OUTPUT-MARK" in content
        for source, content, _ in rows
    )
    assert any(
        source == f"schedule:{at}" and "AT-WAKE-MARK" in content for source, content, _ in rows
    )
    assert any(
        source == f"schedule:{cron}" and "CRON-WAKE-MARK" in content for source, content, _ in rows
    )
    poll_until(
        lambda: _human_witness(spawned_agent, "AT-WAKE-MARK", "CRON-WAKE-MARK"),
        timeout=115.0,
        interval=0.5,
        what="at and cron wakes in model input",
    )


@pytest.mark.scenario("tests.e2e.fakes.scenarios.shell_effects:build_watcher_timeout")
def test_background_ttl_expiry_notifies_owner(spawned_agent: int, clean_shell_world: None) -> None:
    chat_and_wait(spawned_agent, "start bounded watcher")
    wid = int((world.sandbox() / "timeout-id").read_text())

    def timed_out() -> tuple[bool, object]:
        rows = [
            row
            for row in _inbounds(spawned_agent, "system")
            if "e2e-timeout" in row[1] and "TTL expired" in row[1]
        ]
        return bool(rows and rows[0][2] == "done"), rows

    poll_until(timed_out, timeout=60.0, interval=0.3, what="watcher timeout notice")
    _, content, _ = next(
        row
        for row in _inbounds(spawned_agent, "system")
        if "e2e-timeout" in row[1] and "TTL expired" in row[1]
    )
    assert f"id {wid}, agent {spawned_agent}" in content
    assert "interrupting a running task" in content
    poll_until(
        lambda: _human_witness(spawned_agent, "TTL expired"),
        timeout=60.0,
        interval=0.3,
        what="watcher timeout in model input",
    )


@pytest.mark.scenario("tests.e2e.fakes.scenarios.shell_effects:build_watcher_termination")
def test_owner_termination_cancels_at_schedule_before_it_can_fire(
    spawned_agent: int, clean_shell_world: None
) -> None:
    agent = spawned_agent
    chat_and_wait(agent, "arm a schedule before termination")
    sid_text, fire_at_text = (world.sandbox() / "termination-watcher").read_text().splitlines()
    schedule_id, fire_at = int(sid_text), float(fire_at_text)
    schedule = gateway_get(f"{GATEWAY_URL}/api/schedules/{schedule_id}", timeout=10.0)
    schedule.raise_for_status()
    before = schedule.json()
    assert before["owner_agent_id"] == agent
    assert before["trigger_kind"] == "at" and before["enabled"]
    assert before["on_owner_terminate"] == "cancel"

    gateway_post(f"{GATEWAY_URL}/api/agents/{agent}/terminate", timeout=10.0).raise_for_status()
    wait_for_status(agent, "terminated")

    def schedule_cancelled() -> tuple[bool, object]:
        response = gateway_get(f"{GATEWAY_URL}/api/schedules/{schedule_id}", timeout=10.0)
        response.raise_for_status()
        row = response.json()
        return (
            row["status"] == "cancelled" and not row["enabled"] and row["next_launch_at"] is None,
            row,
        )

    poll_until(
        schedule_cancelled,
        timeout=20.0,
        interval=0.3,
        what="termination cancels the owner's schedule and clears its next launch",
    )
    poll_until(
        lambda: (
            time.time() >= fire_at + 2.0,
            {"now": time.time(), "would_have_fired_at": fire_at},
        ),
        timeout=40.0,
        interval=0.3,
        what="the cancelled schedule's original fire time passes",
    )
    assert _inbounds(agent, f"schedule:{schedule_id}") == []
    runs = gateway_get(f"{GATEWAY_URL}/api/schedules/{schedule_id}/runs", timeout=10.0)
    runs.raise_for_status()
    assert runs.json() == []
    with psycopg.connect(settings.data_plane.db_url) as conn:
        status = conn.execute("SELECT status FROM agents_meta WHERE id = %s", (agent,)).fetchone()
    assert status == ("terminated",)


@pytest.mark.scenario("tests.e2e.fakes.scenarios.shell_effects:build_watcher_orphan")
def test_background_child_exits_when_its_session_shell_is_killed(
    spawned_agent: int, clean_shell_world: None
) -> None:
    chat_and_wait(spawned_agent, "start a long watcher")
    pid_path = world.sandbox() / "orphan-pid"
    poll_until(
        lambda: (pid_path.exists(), str(pid_path)),
        timeout=20.0,
        interval=0.2,
        what="watcher child publishes its PID",
    )
    child_pid = int(pid_path.read_text())
    child = psutil.Process(child_pid)
    shell = psutil.Process(child.ppid())
    service = psutil.Process(shell.ppid())
    assert "services.agent_runner.pty_sessions.daemon" in " ".join(service.cmdline())

    def child_dead() -> tuple[bool, object]:
        try:
            state = psutil.Process(child_pid).status()
        except psutil.NoSuchProcess:
            return True, "gone"
        return state == psutil.STATUS_ZOMBIE, state

    try:
        os.kill(shell.pid, signal.SIGKILL)
        poll_until(
            child_dead,
            timeout=20.0,
            interval=0.2,
            what="orphan guard ends watcher child",
        )
    finally:
        if not child_dead()[0]:
            os.kill(child_pid, signal.SIGKILL)
