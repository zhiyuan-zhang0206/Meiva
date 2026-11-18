# meiva:note
# Cold-start subprocess regression tests for `meiva.agents.get_neighbors` (and sibling default-config
# SDK callers `meiva.shell.sessions.capture` and `config_authority()`): a fresh external-harness
# process whose first SDK call omits `depth`/`limit` must attach lazily before reading
# `config_authority()`, resolve the configured defaults, and never fail with
# `the SDK installation carries no ConfigAuthority`.

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
from urllib.parse import parse_qs, urlparse

import psycopg

from base.agents.tests.units import spawn_agent
from base.cluster.machine import MachineIdentityReader
from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from meiva.self.harness.registration import HarnessRegistration, handle_digest
from meiva.self.harness.store import FileRegistrationStore

_REPO_ROOT = Path(__file__).resolve().parents[4]
_PROBE = Path(__file__).with_name("neighbors_coldstart_probe.py")

_CLEAN_ENV_STRIP = frozenset(
    {
        "MEIVA_AGENT_ID",
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
    }
)


def _handle() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


@contextmanager
def _stub_gateway(
    response_body: dict[str, object],
) -> Generator[tuple[str, list[tuple[str, dict[str, list[str]]]]]]:
    seen: list[tuple[str, dict[str, list[str]]]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            seen.append((parsed.path, parse_qs(parsed.query)))
            payload = json.dumps(response_body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            _ = self.wfile.write(payload)

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
# The probe runs a fixed argv from this file; `sys.executable` is the test's own
# interpreter, so the subprocess call carries no shell and no caller input.
def _run_probe(
    mode: str,
    target_id: int,
    *,
    home: Path,
    handle: str | None = None,
    extra_env: dict[str, str] | None = None,
    extra_args: tuple[str, ...] = (),
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
            str(target_id),
            *extra_args,
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
                "MEIVA_NEIGHBORS_DEFAULT_DEPTH=3",
                "MEIVA_NEIGHBORS_DEFAULT_LIMIT=7",
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


def test_cold_harness_get_neighbors_without_depth_or_limit_attaches_and_reads_config_defaults(
    tmp_path: Path,
    db_conn: psycopg.Connection[Any],
    db_url: str,
    redis_url: str,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    del db_conn
    agent_id = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    _seed_checkpoint(db_url, agent_id)
    peer_id = agent_id + 10
    home = tmp_path / "home"
    home.mkdir()
    handle = _handle()
    store = FileRegistrationStore(home / "harness" / "registrations")
    store.save(
        HarnessRegistration(
            agent_id=agent_id,
            harness="codex",
            external_session_id="sess-neighbors-coldstart",
            machine=machine_identity.name(),
            controller_pid=os.getpid(),
            handle_sha256=handle_digest(handle),
            created_at=datetime.now(UTC).isoformat(),
            expires_at=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        )
    )

    body: dict[str, object] = {
        "neighbors": [
            {
                "agent_id": peer_id,
                "label": "peer-agent",
                "status": "running",
                "depth": 2,
                "score": 1.25,
            }
        ],
        "ancestors": [],
    }
    with _stub_gateway(body) as (gateway_url, seen):
        _write_home_env(
            home,
            db_url=db_url,
            redis_url=redis_url,
            gateway_url=gateway_url,
            machine_name=machine_identity.name(),
        )
        env_overrides = {
            "MEIVA_DB_URL": db_url,
            "MEIVA_REDIS_URL": redis_url,
            "MEIVA_GATEWAY_URL": gateway_url,
            "MEIVA_CLUSTER_SECRET": "fixture-coldstart-secret",
            "MEIVA_MACHINE_NAME": machine_identity.name(),
            "MEIVA_NEIGHBORS_DEFAULT_DEPTH": "3",
            "MEIVA_NEIGHBORS_DEFAULT_LIMIT": "7",
        }
        report = _run_probe(
            "neighbors",
            agent_id,
            home=home,
            handle=handle,
            extra_env=env_overrides,
        )
        depth_only_omitted = _run_probe(
            "neighbors",
            agent_id,
            home=home,
            handle=handle,
            extra_env=env_overrides,
            extra_args=("-", "5"),
        )
        limit_only_omitted = _run_probe(
            "neighbors",
            agent_id,
            home=home,
            handle=handle,
            extra_env=env_overrides,
            extra_args=("2", "-"),
        )
        authority_report = _run_probe(
            "config_authority",
            agent_id,
            home=home,
            handle=handle,
            extra_env=env_overrides,
        )
        capture_report = _run_probe(
            "capture_error",
            1,
            home=home,
            handle=handle,
            extra_env=env_overrides,
        )

    assert report["installed_at_import"] is False
    assert report["installed_after"] is True
    assert report["neighbors"] == [
        {
            "agent_id": peer_id,
            "label": "peer-agent",
            "status": "running",
            "depth": 2,
            "score": 1.25,
        }
    ]
    assert depth_only_omitted["installed_after"] is True
    assert limit_only_omitted["installed_after"] is True
    assert seen == [
        (f"/api/agents/{agent_id}/neighbors", {"depth": ["3"], "limit": ["7"]}),
        (f"/api/agents/{agent_id}/neighbors", {"depth": ["3"], "limit": ["5"]}),
        (f"/api/agents/{agent_id}/neighbors", {"depth": ["2"], "limit": ["7"]}),
    ]

    assert authority_report["installed_at_import"] is False
    assert authority_report["installed_after"] is True
    assert authority_report["depth"] == 3

    assert capture_report["installed_at_import"] is False
    assert capture_report["installed_after"] is True
    assert "ConfigAuthority" not in capture_report.get("message", "")


def test_cold_dead_harness_get_neighbors_omitting_defaults_raises_harness_attach_error(
    tmp_path: Path,
) -> None:
    report = _run_probe("neighbors_error", 1, home=tmp_path, handle="hx_deadbeef")
    assert report["installed_at_import"] is False
    assert report["error"] == "HarnessAttachError"
    assert "no harness session matches this handle" in report["message"]


def test_cold_unbound_process_get_neighbors_omitting_defaults_raises_context_outside_process(
    tmp_path: Path,
) -> None:
    report = _run_probe("neighbors_error", 1, home=tmp_path)
    assert report["installed_at_import"] is False
    assert report["error"] == "ContextOutsideProcessError"
    assert "ConfigAuthority" not in report["message"]


def test_cold_unbound_process_shell_capture_omitting_lines_refuses_identity_not_config_authority(
    tmp_path: Path,
) -> None:
    report = _run_probe("capture_error", 1, home=tmp_path)
    assert report["installed_at_import"] is False
    assert report["error"] == "RuntimeError"
    assert "Cannot use shell sessions: this process has no agent identity" in report["message"]
