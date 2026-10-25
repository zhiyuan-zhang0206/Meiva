from __future__ import annotations

import json
import stat
from dataclasses import asdict
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import pytest
from dotenv import dotenv_values

from base import cluster
from scripts.data_plane_ops import rotate_data_plane_secrets as rotate

_OLD_REDIS_ADMIN = "old-redis-admin"
_NEW_REDIS_ADMIN = "new-redis-admin"
_OLD_REDIS_RUNTIME = "old-redis-runtime"
_NEW_REDIS_RUNTIME = "new-redis-runtime"
_ENV = f"MEIVA_REDIS_ADMIN_PASSWORD={_OLD_REDIS_ADMIN}\nMEIVA_REDIS_PASSWORD={_OLD_REDIS_RUNTIME}\n"


def _state(scope: rotate.RotationScope = rotate.RotationScope.BOTH) -> rotate.RotationState:
    return rotate.RotationState(
        scope=scope,
        old_redis_admin_password=_OLD_REDIS_ADMIN,
        new_redis_admin_password=_NEW_REDIS_ADMIN,
        old_redis_password=_OLD_REDIS_RUNTIME,
        new_redis_password=_NEW_REDIS_RUNTIME,
        redis_port=16380,
        redis_host="127.0.0.1",
        redis_user="meiva",
    )


def _context(
    home: Path, *, profile: str | None = None, is_remote: bool = False
) -> rotate.GatewayContext:
    return rotate.GatewayContext(
        home=home,
        profile=profile,
        redis_url="redis://meiva:old-redis-runtime@127.0.0.1:16380/0",
        events_channel="meiva:events",
        is_remote=is_remote,
    )


def _record(home: Path) -> cluster.ClusterRecord:
    return cluster.ClusterRecord(
        ports=cast("cluster.ClusterPorts", {"postgres": 15433, "redis": 16380}),
        gateway_home=str(home),
        created_at="now",
    )


def test_build_state_mints_fresh_redis_values_even_without_a_bearer(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(_ENV)

    state = rotate.build_state(_context(tmp_path), _record(tmp_path))

    assert state.scope == "both"
    assert state.new_redis_admin_password not in {"", state.old_redis_admin_password}
    assert state.new_redis_password not in {"", state.old_redis_password}
    assert (state.redis_port, state.redis_user) == (16380, "meiva")


def test_build_state_refuses_a_home_without_redis_credentials(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(f"MEIVA_REDIS_ADMIN_PASSWORD={_OLD_REDIS_ADMIN}\n")
    with pytest.raises(RuntimeError, match="no conversion exists"):
        rotate.build_state(_context(tmp_path), _record(tmp_path))


def test_build_state_refuses_an_agent_process_profile(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(_ENV)
    with pytest.raises(RuntimeError, match="gateway context"):
        rotate.build_state(_context(tmp_path, profile="agent"), _record(tmp_path))


@pytest.mark.parametrize("resume", [False, True])
def test_main_reports_an_agent_context_guard_cleanly(
    resume: bool,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = ["--resume", str(tmp_path / "missing-state.json")] if resume else []

    assert (
        rotate.run_rotation(
            args,
            context=_context(tmp_path, profile="agent"),
            record=_record(tmp_path),
            started_at="2026-10-25T00:00:00+00:00",
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "gateway context" in err
    assert "unset MEIVA_PROCESS_PROFILE" in err
    assert "Traceback" not in err


def test_admin_scope_changes_the_redis_default_user_only() -> None:
    state = _state(rotate.RotationScope.ADMIN)

    assert rotate.admin_requirepass_command(state) == (
        "CONFIG",
        "SET",
        "requirepass",
        _NEW_REDIS_ADMIN,
    )
    assert rotate.runner_acl_plan(state, admin_password=_OLD_REDIS_ADMIN) is None
    assert state.redis_password == _OLD_REDIS_RUNTIME


def test_runner_scope_rotates_the_acl_user_of_the_redis_url() -> None:
    state = _state(rotate.RotationScope.RUNNER)

    assert rotate.admin_requirepass_command(state) == ()
    assert rotate.runner_acl_plan(
        state,
        admin_password=_OLD_REDIS_ADMIN,
        events_channel="meiva:events",
    ) == rotate.RunnerAclPlan(
        user="meiva",
        redis_admin_url=f"redis://default:{_OLD_REDIS_ADMIN}@127.0.0.1:16380",
        runtime_password=_NEW_REDIS_RUNTIME,
        channel_prefix="meiva",
    )


def test_write_env_syncs_the_redis_url_with_the_active_passwords(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(_ENV)
    state = _state(rotate.RotationScope.RUNNER)

    rotate.write_env(state, _context(tmp_path))

    written = dotenv_values(tmp_path / ".env")
    assert written["MEIVA_REDIS_ADMIN_PASSWORD"] == _OLD_REDIS_ADMIN
    assert written["MEIVA_REDIS_PASSWORD"] == _NEW_REDIS_RUNTIME
    assert urlsplit(written["MEIVA_REDIS_URL"] or "").password == _NEW_REDIS_RUNTIME


def test_preflight_and_verify_probe_the_state_host_as_the_url_user() -> None:
    state = _state()
    state.redis_host = "10.0.0.7"

    probes = [
        (spec.host, spec.username, spec.password)
        for spec in (*rotate.preflight_probes(state), *rotate.verify_probes(state))
    ]
    assert probes == [
        ("10.0.0.7", "default", _OLD_REDIS_ADMIN),
        ("10.0.0.7", "meiva", _OLD_REDIS_RUNTIME),
        ("10.0.0.7", "default", _NEW_REDIS_ADMIN),
        ("10.0.0.7", "meiva", _NEW_REDIS_RUNTIME),
    ]


def test_dry_run_never_calls_a_mutating_phase() -> None:
    assert rotate.rotation_phases(execute=False) == ()
    assert rotate.rotation_phases(execute=True) == (
        rotate.RotationPhase.ADMIN,
        rotate.RotationPhase.RUNNER,
        rotate.RotationPhase.VERIFIED,
        rotate.RotationPhase.ENV_WRITTEN,
    )


def test_recovery_state_is_owner_only(tmp_path: Path) -> None:
    state = _state()
    path = state.save(tmp_path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert rotate.RotationState.load(path) == state


def test_a_postgres_era_journal_is_refused_by_name(tmp_path: Path) -> None:
    data = {**asdict(_state()), "old_db_admin_password": "x", "pg_port": 5433}
    path = tmp_path / "data-plane-old.json"
    path.write_text(json.dumps(data))
    with pytest.raises(RuntimeError, match="not a Redis-only rotation journal"):
        rotate.RotationState.load(path)


def test_main_refuses_remote_managed_data_plane(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        rotate.run_rotation(
            ["--scope", "admin"],
            context=_context(tmp_path, is_remote=True),
            record=_record(tmp_path),
            started_at="2026-10-25T00:00:00+00:00",
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "remote-managed" in err
    assert "rotates credentials at the provider" in err
