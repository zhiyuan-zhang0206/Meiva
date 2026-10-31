from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from services.backup.walg import runner
from services.backup.walg.config import WalgConfigError
from services.backup.walg.tests.support import Sandbox, make_sandbox


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sandbox:
    return make_sandbox(tmp_path, monkeypatch)


# meiva:note
# A fresh interpreter whose own environment holds Meiva authority and a storage secret.
def test_the_environment_carries_process_mechanics_and_no_meiva_authority() -> None:
    inherited = {
        **os.environ,
        "MEIVA_API_TOKEN": "token-value",
        "MEIVA_TEST_ONLY_SECRET": "secret-value",
        "OSS_ACCESS_KEY_SECRET": "ambient-secret",
    }

    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(Path(__file__).with_name("child_walg.py")), "env"],
        env=inherited,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )

    env = json.loads(proc.stdout)
    assert "PATH" in env
    assert not [name for name in env if name.startswith("MEIVA_")]
    assert "OSS_ACCESS_KEY_SECRET" not in env, "storage credentials come from the 0600 file only"


# meiva:note
# The environment a wal-g child gets is the mapping its caller hands in, filtered to the daemon
# mechanics: a value this process carries but the caller did not pass must never reach the child,
# and a credential in the passed mapping must not either.
def test_the_child_environment_is_the_mapping_the_caller_hands_in() -> None:
    env = runner.walg_env(
        environ={"PATH": "/only-the-caller/bin", "MEIVA_API_TOKEN": "token-value"}
    )

    assert env.get("PATH") == "/only-the-caller/bin", (
        "the child's PATH must be the caller's: a wal-g call that read this process's own "
        "environment instead would run the developer's binary search path, and every caller "
        "that threaded the value down would be doing it for nothing"
    )
    assert not [name for name in env if name.startswith("MEIVA_")], (
        "no Meiva authority may cross into a wal-g child: the mapping is filtered to the daemon "
        "mechanics, so a token the caller's environment carries must be dropped"
    )


def test_postgres_connection_variables_name_the_owner_only_socket() -> None:
    env = runner.walg_env(
        environ={"PATH": "/usr/bin:/bin"},
        pg_admin_url="postgresql://zzy@/postgres?host=/sockets/meiva-pg-home&port=5433",
        extra={"WALG_DELTA_MAX_STEPS": "6"},
    )

    assert (env["PGHOST"], env["PGPORT"], env["PGUSER"], env["PGDATABASE"]) == (
        "/sockets/meiva-pg-home",
        "5433",
        "zzy",
        "postgres",
    )
    assert env["WALG_DELTA_MAX_STEPS"] == "6"
    assert "PGPASSWORD" not in env


def test_the_call_is_wal_g_with_the_config_file_and_the_given_arguments(sandbox: Sandbox) -> None:
    runner.run_walg(
        ["st", "ls"], timeout_s=30, path=sandbox.path, home=sandbox.home, environ=sandbox.environ
    )

    assert sandbox.calls() == ["st ls"]


def test_json_output_is_parsed(sandbox: Sandbox) -> None:
    assert runner.run_walg(
        ["backup-list", "--json"],
        timeout_s=30,
        as_json=True,
        path=sandbox.path,
        home=sandbox.home,
        environ=sandbox.environ,
    ) == [{"backup_name": "base_000000010000000000000002"}]


def test_a_failing_call_reports_the_exit_code_and_stderr_tail(sandbox: Sandbox) -> None:
    sandbox.set_mode("fail")

    with pytest.raises(
        runner.WalgCommandError, match=r"wal-g st failed \(exit 1\).*simulated failure"
    ):
        runner.run_walg(
            ["st", "ls"],
            timeout_s=30,
            path=sandbox.path,
            home=sandbox.home,
            environ=sandbox.environ,
        )


def test_a_hung_call_is_cut_off(sandbox: Sandbox) -> None:
    sandbox.set_mode("hang")

    with pytest.raises(runner.WalgCommandError, match="did not finish within 1s"):
        runner.run_walg(
            ["st", "ls"], timeout_s=1, path=sandbox.path, home=sandbox.home, environ=sandbox.environ
        )
    sandbox.set_mode("ok")


def test_off_means_no_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox = make_sandbox(tmp_path, monkeypatch, enabled=False)

    with pytest.raises(WalgConfigError, match="MEIVA_WALG_CONFIG_FILE is not set"):
        runner.run_walg(
            ["st", "ls"],
            timeout_s=30,
            path=sandbox.path,
            home=sandbox.home,
            environ=sandbox.environ,
        )

    assert sandbox.calls() == []


def test_the_logged_variant_returns_both_streams(sandbox: Sandbox) -> None:
    sandbox.put(
        "delete-dry.log", "INFO: Object marked for deletion: wal_005/X.lz4 storage=default\n"
    )

    output = runner.run_walg_logged(
        ["delete", "retain", "FULL", "3"],
        timeout_s=30,
        path=sandbox.path,
        home=sandbox.home,
        environ=sandbox.environ,
    )

    assert output.stdout == ""
    assert "Object marked for deletion: wal_005/X.lz4" in output.stderr
