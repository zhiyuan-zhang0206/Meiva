# meiva:note
# The service roster derives its probe URLs, ports and launch commands from the unit's settings.
#
# The roster reads process settings when it is built, so each case builds it in a child process
# (`roster_probe_child.py`) whose environment carries the settings under test: the watchdog probe
# URLs follow the endpoint table, and memory-search's tcp_port, the frontend's curl URL and
# launch command and the gateway's probe URL are derived from settings rather than hardcoded.


from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_CHILD = Path(__file__).with_name("roster_probe_child.py")
_REPO = Path(__file__).resolve().parents[3]

_EXPECTED_SESSIONS = {
    "gate",
    "gateway",
    "im-bridge",
    "labeler",
    "heartbeat",
    "delivery-watchdog",
    "events-maintenance",
    "task-maintenance",
    "memory-search",
    "memory-indexer",
    "frontend",
    "ops",
    "browser",
    "browser-mcp",
    "mcp-daemon",
    "pty-sessions",
    "computer-mcp",
    "page-server",
    "pg-backup",
    "redis-bridge",
    "ttl-reaper",
    "schedule-manager",
    "insights",
    "otel-collector",
    "agent-host",
    "exec-host",
    "loki",
    "prometheus",
    "grafana",
}


# meiva:note
# The roster as a child process built it under `home` with `env` as its only MEIVA_* settings.
# A host-scope setting reaches a process through its environment and a cluster-scope one only
# through the unit's own `.env`, so each value is delivered both ways.
def _roster_in_child(home: Path, env: dict[str, str]) -> dict[str, object]:
    (home / ".env").write_text("".join(f"{key}={value}\n" for key, value in env.items()))
    base_env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR") if key in os.environ}
    done = subprocess.run(
        [sys.executable, str(_CHILD)],
        cwd=_REPO,
        env={**base_env, "MEIVA_HOME": str(home), **env},
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def entry_port_roster(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    """No explicit app port: the frontend serves one port above its entry port."""
    return _roster_in_child(
        tmp_path_factory.mktemp("entry-port"),
        {
            "MEIVA_LABELER_HEALTH_PORT": "18003",
            "MEIVA_OPS_HEALTH_PORT": "18106",
            "MEIVA_MEMORY_SEARCH_PORT": "29531",
            "MEIVA_FRONTEND_HEALTHCHECK_URL": "http://localhost:4000",
            "MEIVA_GATEWAY_HEALTH_URL": "http://localhost:9999/api/agents",
            "MEIVA_BROWSER_ENABLED": "true",
        },
    )


@pytest.fixture(scope="module")
def explicit_port_roster(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    return _roster_in_child(
        tmp_path_factory.mktemp("explicit-port"),
        {
            "MEIVA_FRONTEND_HEALTHCHECK_URL": "http://localhost:4000",
            "MEIVA_APP_PORT": "4800",
            "MEIVA_GATEWAY_PORT": "8800",
        },
    )


# meiva:note
# Pinning the 'labeler' port must be reflected in the roster.
def test_daemon_probe_url_follows_health_port(entry_port_roster: dict[str, object]) -> None:
    labeler = entry_port_roster["labeler_curl_url"]
    assert isinstance(labeler, str)
    assert "18003" in labeler


def test_ops_probe_follows_health_port(entry_port_roster: dict[str, object]) -> None:
    ops = entry_port_roster["ops_curl_url"]
    assert isinstance(ops, str)
    assert "18106" in ops


def test_memory_search_tcp_port_follows_settings(entry_port_roster: dict[str, object]) -> None:
    assert entry_port_roster["memory_search_tcp_port"] == 29531


# meiva:note
# The frontend healthcheck probes the Next.js app — MEIVA_APP_PORT when set,
#     else the entry port + 1 (the entry itself is owned by the gate).
def test_frontend_curl_url_follows_app_port(entry_port_roster: dict[str, object]) -> None:
    assert entry_port_roster["frontend_curl_url"] == "http://localhost:4001"


def test_frontend_curl_url_uses_explicit_app_port(
    explicit_port_roster: dict[str, object],
) -> None:
    assert explicit_port_roster["frontend_curl_url"] == "http://localhost:4800"


# meiva:note
# Frontend launch cmd passes -p <app port> (MEIVA_APP_PORT or entry+1).
def test_frontend_cmd_port_follows_app_port(entry_port_roster: dict[str, object]) -> None:
    assert "-p 4001" in str(entry_port_roster["frontend_cmd"])


def test_frontend_cmd_port_uses_explicit_app_port(
    explicit_port_roster: dict[str, object],
) -> None:
    assert "-p 4800" in str(explicit_port_roster["frontend_cmd"])


def test_frontend_build_injects_gateway_port(explicit_port_roster: dict[str, object]) -> None:
    # meiva:note
    # NEXT_PUBLIC_* are baked into the JS bundle at build time and the session only
    # forwards MEIVA_* env, so the build subprocess never sees a .env-only
    # NEXT_PUBLIC_GATEWAY_PORT; it must ride the command line from the single source
    # of truth (MEIVA_GATEWAY_PORT), and the build guard must receive the same value
    # so a changed port is a changed build key.
    # meiva:link base/cluster/docs/frontend-build.meiva.okf.md
    cmd = str(explicit_port_roster["frontend_cmd"])
    assert "NEXT_PUBLIC_GATEWAY_PORT=8800" in cmd
    assert ".venv/bin/python -m base.cluster.frontend_build ensure" in cmd
    assert "--gateway-port 8800" in cmd


# meiva:note
# Gateway probe URL must follow settings.services.gateway_health_url.
def test_gateway_curl_url_follows_settings(entry_port_roster: dict[str, object]) -> None:
    assert entry_port_roster["gateway_curl_url"] == "http://localhost:9999/api/agents"


# meiva:note
# The roster must return the expected complete service set.
def test_all_services_present(entry_port_roster: dict[str, object]) -> None:
    assert set(entry_port_roster["sessions"]) == _EXPECTED_SESSIONS  # pyright: ignore[reportArgumentType]
