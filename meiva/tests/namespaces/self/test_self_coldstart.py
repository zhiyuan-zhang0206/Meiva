# meiva:note
# Cold-start subprocess regression tests for `meiva.self.*` and `agent_identity` / `settings`:
# 1. A fresh external-harness process that has never touched `meiva.self.AGENT_ID` attaches lazily
#    on its first `meiva.self.restart(...)` (and sibling `meiva.self.*` calls), routing restart to
#    the deferred turn-end gateway path instead of falling back to the native harness.
# 2. When external-harness attachment fails (e.g. missing/expired registration or transient DB
#    error), `process_harness_name()`, `assert_self_action()`, `meiva.self.*`, and `settings`
#    surface `HarnessAttachError` instead of swallowing it and raising `agent id is unset` or
#    `the SDK installation carries no ConfigAuthority`.
# 3. A truly non-agent process (no harness handle, no `MEIVA_AGENT_ID`) preserves its original
#    missing-identity refusal semantics across `meiva.self.*`.

from __future__ import annotations

import base64
import json
import os
import secrets
import subprocess
import sys
import threading
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest

from base.agents.tests.units import spawn_agent
from base.cluster.machine import MachineIdentityReader
from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from meiva.self.harness.registration import HarnessRegistration, handle_digest
from meiva.self.harness.store import FileRegistrationStore

_REPO_ROOT = Path(__file__).resolve().parents[4]
_PROBE = Path(__file__).with_name("self_coldstart_probe.py")

_CLEAN_ENV_STRIP = frozenset(
    {
        "MEIVA_AGENT_ID",
        "MEIVA_SCHEDULE_ID",
        "MEIVA_PROCESS_PROFILE",
        "MEIVA_EXEC_REQUEST_FILE",
        "MEIVA_EXEC_RESULT_FILE",
        "MEIVA_EXEC_TIMEOUT_S",
        "MEIVA_TURN_ID",
        "MEIVA_SESSION_ID",
        "MEIVA_LOG_DIR",
        "MEIVA_AGENT_LABEL",
        "MEIVA_AGENT_DIR",
        "MEIVA_HARNESS_HANDLE",
        "MEIVA_HARNESS",
    }
)


def _handle() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


@contextmanager
def _stub_gateway() -> Generator[tuple[str, list[tuple[str, str, dict[str, object]]]]]:
    seen: list[tuple[str, str, dict[str, object]]] = []

    class Handler(BaseHTTPRequestHandler):
        def _respond(self, method: str) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            body = cast("dict[str, object]", json.loads(raw.decode("utf-8")))
            seen.append((method, self.path, body))
            response: dict[str, object] = {"status": "enqueued"} if method == "POST" else {}
            payload = json.dumps(response).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            _ = self.wfile.write(payload)

        def do_POST(self) -> None:
            self._respond("POST")

        def do_PATCH(self) -> None:
            self._respond("PATCH")

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host!s}:{port}", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# meiva:note
# The probe runs a fixed argv from this file; `sys.executable` is the test's own interpreter, so
# the subprocess call carries no shell and no caller input.
def _run_probe(
    mode: str,
    *,
    home: Path,
    handle: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    env = {key: value for key, value in os.environ.items() if key not in _CLEAN_ENV_STRIP}
    env["MEIVA_HOME"] = str(home)
    env["MEIVA_CONFIG_FETCH"] = "skip"
    if handle is not None:
        env["MEIVA_HARNESS_HANDLE"] = handle
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-I",
            "-B",
            "-X",
            "utf8",
            str(_PROBE),
            str(_REPO_ROOT),
            mode,
        ],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    line = next(line for line in proc.stdout.splitlines() if line.startswith("REPORT "))
    return cast("dict[str, Any]", json.loads(line.removeprefix("REPORT ")))


def _write_home_env(
    home: Path,
    *,
    db_url: str,
    redis_url: str,
    gateway_url: str,
    machine_name: str,
) -> None:
    _ = (home / ".env").write_text(
        "\n".join(
            [
                f"MEIVA_DB_URL={db_url}",
                f"MEIVA_REDIS_URL={redis_url}",
                f"MEIVA_GATEWAY_URL={gateway_url}",
                "MEIVA_CLUSTER_SECRET=fixture-coldstart-secret",
                f"MEIVA_MACHINE_NAME={machine_name}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _seed_checkpoint(db_url: str, agent_id: int) -> None:
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.base import empty_checkpoint
    from langgraph.checkpoint.postgres import PostgresSaver

    with PostgresSaver.from_conn_string(db_url) as saver:
        cfg: RunnableConfig = {"configurable": {"thread_id": str(agent_id), "checkpoint_ns": ""}}
        _ = saver.put(cfg, empty_checkpoint(), {}, {})


def _setup_live_harness_home(
    tmp_path: Path,
    *,
    db_url: str,
    redis_url: str,
    gateway_url: str,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
    harness: str = "claude_code",
) -> tuple[int, Path, str, dict[str, str]]:
    agent_id = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    _seed_checkpoint(db_url, agent_id)
    home = tmp_path / f"home-{harness}"
    home.mkdir()
    handle = _handle()
    store = FileRegistrationStore(home / "harness" / "registrations")
    store.save(
        HarnessRegistration(
            agent_id=agent_id,
            harness=harness,
            external_session_id=f"sess-self-coldstart-{harness}",
            machine=machine_identity.name(),
            controller_pid=os.getpid(),
            handle_sha256=handle_digest(handle),
            created_at=datetime.now(UTC).isoformat(),
            expires_at=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        )
    )
    _write_home_env(
        home,
        db_url=db_url,
        redis_url=redis_url,
        gateway_url=gateway_url,
        machine_name=machine_identity.name(),
    )
    extra_env = {
        "MEIVA_DB_URL": db_url,
        "MEIVA_REDIS_URL": redis_url,
        "MEIVA_GATEWAY_URL": gateway_url,
        "MEIVA_CLUSTER_SECRET": "fixture-coldstart-secret",
        "MEIVA_MACHINE_NAME": machine_identity.name(),
    }
    return agent_id, home, handle, extra_env


def _assert_cold_attached(report: dict[str, Any]) -> None:
    assert (report["bound_before"], report["installed_at_import"], report["bound_after"]) == (
        False,
        False,
        True,
    )


def _assert_native_only_refused(report: dict[str, Any], harness: str) -> None:
    _assert_cold_attached(report)
    assert report["error"] == "RuntimeError"
    assert "only supported on the native 'meiva' harness" in report["message"]
    assert harness in report["message"]


def test_cold_external_harness_self_family_attaches_lazily_without_prior_agent_id_access(
    tmp_path: Path,
    db_conn: psycopg.Connection[Any],
    db_url: str,
    redis_url: str,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    with _stub_gateway() as (gateway_url, seen):
        agent_id, home, handle, extra_env = _setup_live_harness_home(
            tmp_path,
            db_url=db_url,
            redis_url=redis_url,
            gateway_url=gateway_url,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            machine_identity=machine_identity,
            harness="claude_code",
        )
        reports = {
            mode: _run_probe(mode, home=home, handle=handle, extra_env=extra_env)
            for mode in (
                "restart",
                "restart_switch",
                "pause_heartbeat",
                "set_label",
                "compact",
                "attach",
                "terminate",
            )
        }

    for mode in ("restart", "restart_switch", "pause_heartbeat", "set_label", "terminate"):
        _assert_cold_attached(reports[mode])
        assert "error" not in reports[mode], reports[mode]

    assert "applied after the current turn ends, not now" in reports["restart"]["result"]
    assert "your harness becomes 'antigravity'" in reports["restart_switch"]["result"]
    assert (reports["pause_heartbeat"]["result"], reports["set_label"]["result"]) == ("ok", "ok")
    assert reports["terminate"]["exit_code"] == 0
    _assert_native_only_refused(reports["compact"], "claude_code")
    _assert_native_only_refused(reports["attach"], "claude_code")

    pause_rows = db_conn.execute(
        "SELECT duration_s FROM heartbeat_pause_log WHERE agent_id = %s", (agent_id,)
    ).fetchall()
    terminate_rows = db_conn.execute(
        "SELECT source, content, payload FROM inbound_messages "
        "WHERE agent_id = %s AND kind = 'terminate'",
        (agent_id,),
    ).fetchall()
    assert (pause_rows, terminate_rows) == ([(90.0,)], [("self", "", None)])
    assert seen == [
        (
            "POST",
            f"/api/agents/{agent_id}/restart",
            {"source": f"agent:{agent_id}", "message": "cold restart", "at_turn_end": True},
        ),
        (
            "POST",
            f"/api/agents/{agent_id}/restart",
            {
                "source": f"agent:{agent_id}",
                "message": "cold switch",
                "config_overlay": {"harness": "antigravity", "llm_model": "gemini-4-neon"},
                "at_turn_end": True,
            },
        ),
        (
            "PATCH",
            f"/api/agents/{agent_id}",
            {"label": "cold-label", "source": "self"},
        ),
    ]


def test_cold_live_registration_with_db_failure_surfaces_harness_attach_error_not_agent_id_unset(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home-db-down"
    home.mkdir()
    handle = _handle()
    machine_name = "cold-probe-machine"
    store = FileRegistrationStore(home / "harness" / "registrations")
    store.save(
        HarnessRegistration(
            agent_id=6111,
            harness="claude_code",
            external_session_id="sess-db-down",
            machine=machine_name,
            controller_pid=os.getpid(),
            handle_sha256=handle_digest(handle),
            created_at=datetime.now(UTC).isoformat(),
            expires_at=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        )
    )
    dead_db_url = "postgresql://meiva@127.0.0.1:1/meiva_unreachable?connect_timeout=1"
    _write_home_env(
        home,
        db_url=dead_db_url,
        redis_url="redis://127.0.0.1:1/0",
        gateway_url="http://127.0.0.1:1",
        machine_name=machine_name,
    )
    extra_env = {
        "MEIVA_DB_URL": dead_db_url,
        "MEIVA_REDIS_URL": "redis://127.0.0.1:1/0",
        "MEIVA_GATEWAY_URL": "http://127.0.0.1:1",
        "MEIVA_CLUSTER_SECRET": "fixture-coldstart-secret",
        "MEIVA_MACHINE_NAME": machine_name,
    }
    report = _run_probe("restart", home=home, handle=handle, extra_env=extra_env)
    assert report["bound_before"] is False
    assert report["error"] == "HarnessAttachError", report
    assert "agent id is unset" not in report["message"]
    assert "no harness session matches" not in report["message"]


@pytest.mark.parametrize(
    "mode",
    [
        "restart",
        "restart_switch",
        "terminate",
        "pause_heartbeat",
        "set_label",
        "compact",
        "attach",
        "process_harness_name",
        "assert_self_action",
        "config_authority",
        "clock",
        "rng",
    ],
)
def test_cold_failed_harness_attach_surfaces_harness_attach_error_never_agent_id_unset(
    tmp_path: Path, mode: str
) -> None:
    report = _run_probe(mode, home=tmp_path, handle="hx_deadbeef")
    assert report["bound_before"] is False
    assert report["installed_at_import"] is False
    assert report["error"] == "HarnessAttachError", report
    assert "no harness session matches this handle" in report["message"]
    assert "agent id is unset" not in report["message"]
    assert "ConfigAuthority" not in report["message"]


@pytest.mark.parametrize("mode", ["restart", "restart_switch", "terminate", "compact"])
def test_unbound_non_agent_process_still_refuses_self_lifecycle_actions(
    tmp_path: Path, mode: str
) -> None:
    report = _run_probe(mode, home=tmp_path)
    assert report["bound_before"] is False
    assert report["error"] == "RuntimeError", report
    assert "needs an established agent identity" in report["message"]
    assert "agent id is unset" in report["message"]


@pytest.mark.parametrize("mode", ["pause_heartbeat", "set_label"])
def test_unbound_non_agent_process_refuses_identity_before_settings(
    tmp_path: Path, mode: str
) -> None:
    report = _run_probe(mode, home=tmp_path)
    assert report["bound_before"] is False
    assert report["error"] == "RuntimeError", report
    assert "no established agent identity" in report["message"]
    assert "ConfigAuthority" not in report["message"]


def test_unbound_non_agent_process_harness_name_stays_native_default(tmp_path: Path) -> None:
    report = _run_probe("process_harness_name", home=tmp_path)
    assert report["bound_before"] is False
    assert "error" not in report
    assert report["result"] == "meiva"
