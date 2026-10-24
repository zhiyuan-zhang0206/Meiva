# meiva:note
# CLI normalization records launcher context before the real authority pass reads its home.

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from base.host.proc import run_bounded


@pytest.mark.parametrize(
    ("live", "recorded", "expected_record", "keep", "refused"),
    [
        pytest.param("agent", None, "agent", True, False, id="live-agent"),
        pytest.param(None, "agent", "agent", True, False, id="recorded-agent"),
        pytest.param("agent", "gateway", "agent", True, False, id="live-overwrites-record"),
        pytest.param("gateway", None, "gateway", False, False, id="gateway"),
        pytest.param("runner", None, "runner", False, False, id="runner"),
        pytest.param(None, None, None, False, False, id="no-launcher"),
        pytest.param("", "agent", "agent", True, False, id="empty-live-keeps-record"),
        pytest.param(None, "", "", False, False, id="empty-record"),
        pytest.param("agnet", None, "agnet", False, True, id="unknown-live"),
        pytest.param(None, "Runner", "Runner", False, True, id="unknown-record"),
    ],
)
def test_cli_entry_records_launcher_profile_and_keeps_undeclared_projections(
    tmp_path: Path,
    live: str | None,
    recorded: str | None,
    expected_record: str | None,
    keep: bool,
    refused: bool,
) -> None:
    user_home = tmp_path / "user"
    user_home.mkdir()
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    env_file = home / ".env"
    env_file.write_text(
        "MEIVA_AGENT_HOST_HEALTH_PORT=18035\n"
        "MEIVA_CLUSTER_SECRET=synthetic-cluster-secret\n"
        "MEIVA_GATEWAY_URL=http://gateway.invalid:8000\n"
    )
    before = (env_file.read_bytes(), env_file.stat().st_mtime_ns)
    env = {
        "HOME": str(user_home),
        "MEIVA_HOME": str(home),
        "MEIVA_CONFIG_FETCH": "skip",
        "PATH": "/usr/bin:/bin",
        "MEIVA_DB_URL": "postgresql://meiva_runner:synthetic-runner@db.invalid:5433/meiva",
        "MEIVA_REDIS_URL": "redis://meiva:synthetic-runtime@redis.invalid:6380/0",
        "MEIVA_APP_PORT": "3001",
    }
    if live is not None:
        env["MEIVA_PROCESS_PROFILE"] = live
    if recorded is not None:
        env["MEIVA_LAUNCHER_PROFILE"] = recorded

    child = run_bounded(
        [sys.executable, "-I", "-B", "-m", "cli.tests.guards.launcher_context_child"],
        cwd=user_home,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert (env_file.read_bytes(), env_file.stat().st_mtime_ns) == before
    assert sorted(path.name for path in home.iterdir()) == [".env"]
    lines = child.stdout.splitlines()
    assert lines, child.stderr
    assert json.loads(lines[0]) == {
        "profile": None,
        "recorded": expected_record,
        "nested_recorded": expected_record,
    }
    if refused:
        assert child.returncode != 0
        assert "not a known process profile" in child.stderr
        assert len(lines) == 1, "an unknown profile must not complete the authority pass"
    else:
        assert child.returncode == 0, child.stderr
        assert len(lines) == 2
        assert json.loads(lines[1]) == {
            "db_present": keep,
            "db_kept": keep,
            "redis_present": keep,
            "redis_kept": keep,
            "app_port_present": False,
        }
