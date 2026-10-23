from __future__ import annotations

import json
import stat
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from urllib.parse import urlsplit

import pytest

from base import cluster
from base.config import settings
from scripts.data_plane_ops import rotate_data_plane_secrets as rotate

_OLD_REDIS_ADMIN = "old-redis-admin"
_NEW_REDIS_ADMIN = "new-redis-admin"
_OLD_REDIS_RUNTIME = "old-redis-runtime"
_NEW_REDIS_RUNTIME = "new-redis-runtime"
_ENV = f"MEIVA_REDIS_ADMIN_PASSWORD={_OLD_REDIS_ADMIN}\nMEIVA_REDIS_PASSWORD={_OLD_REDIS_RUNTIME}\n"


def _state(scope: str = "both") -> rotate.RotationState:
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


def _gateway_cfg(
    *,
    profile: str | None = None,
    is_remote: bool = False,
    redis_url: str = "redis://meiva:old-redis-runtime@127.0.0.1:16380/0",
) -> SimpleNamespace:
    return SimpleNamespace(
        profile=profile,
        data_plane=SimpleNamespace(
            cluster_secret="",
            db_url="postgresql://meiva@127.0.0.1:16433/meiva",
            redis_url=redis_url,
            events_channel=settings.data_plane.events_channel,
            is_remote=is_remote,
        ),
    )


def _record(home: Path) -> cluster.ClusterRecord:
    return cluster.ClusterRecord(
        ports=cast("cluster.ClusterPorts", {"postgres": 15433, "redis": 16380}),
        gateway_home=str(home),
        created_at="now",
    )


def test_build_state_mints_fresh_redis_values_even_without_a_bearer(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(_ENV)

    state = rotate.build_state(
        cfg=_gateway_cfg(),
        home_fn=lambda: tmp_path,
        record_fn=_record,
    )

    assert state.scope == "both"
    assert state.new_redis_admin_password not in {"", state.old_redis_admin_password}
    assert state.new_redis_password not in {"", state.old_redis_password}
    assert (state.redis_port, state.redis_user) == (16380, "meiva")


def test_build_state_refuses_a_home_without_redis_credentials(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(f"MEIVA_REDIS_ADMIN_PASSWORD={_OLD_REDIS_ADMIN}\n")
    with pytest.raises(RuntimeError, match="no conversion exists"):
        rotate.build_state(
            cfg=_gateway_cfg(),
            home_fn=lambda: tmp_path,
            record_fn=_record,
        )


def test_build_state_refuses_an_agent_process_profile(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(_ENV)
    with pytest.raises(RuntimeError, match="gateway context"):
        rotate.build_state(
            cfg=_gateway_cfg(profile="agent"),
            home_fn=lambda: tmp_path,
            record_fn=_record,
        )


@pytest.mark.parametrize("resume", [False, True])
def test_main_reports_an_agent_context_guard_cleanly(
    resume: bool,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = ["--resume", str(tmp_path / "missing-state.json")] if resume else []

    assert rotate.main(args, cfg=_gateway_cfg(profile="agent")) == 1
    err = capsys.readouterr().err
    assert "gateway context" in err
    assert "unset MEIVA_PROCESS_PROFILE" in err
    assert "Traceback" not in err


def test_admin_scope_changes_the_redis_default_user_only() -> None:
    state = _state("admin")
    redis_commands: list[tuple[str, ...]] = []

    class _Redis:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> _Redis:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute_command(self, *args: str) -> None:
            redis_commands.append(args)

    def _working_redis_admin_password(_state: rotate.RotationState) -> str:
        return _OLD_REDIS_ADMIN

    rotate.apply_admin(
        state,
        admin_pw_fn=_working_redis_admin_password,
        redis_cls=_Redis,
    )
    rotate.apply_runner(
        state,
        admin_pw_fn=_working_redis_admin_password,
    )

    assert redis_commands == [("CONFIG", "SET", "requirepass", _NEW_REDIS_ADMIN)]
    assert state.redis_password == _OLD_REDIS_RUNTIME


def test_runner_scope_rotates_the_acl_user_of_the_redis_url() -> None:
    state = _state("runner")
    acl_calls: list[tuple[str, dict[str, object]]] = []

    def _working_redis_admin_password(_state: rotate.RotationState) -> str:
        return _OLD_REDIS_ADMIN

    def _ensure_cluster_redis_acl(user: str, **kwargs: object) -> None:
        acl_calls.append((user, kwargs))

    rotate.apply_admin(state, admin_pw_fn=_working_redis_admin_password)
    rotate.apply_runner(
        state,
        admin_pw_fn=_working_redis_admin_password,
        ensure_acl_fn=_ensure_cluster_redis_acl,
    )

    assert acl_calls == [
        (
            "meiva",
            {
                "redis_admin_url": f"redis://default:{_OLD_REDIS_ADMIN}@127.0.0.1:16380",
                "runtime_password": _NEW_REDIS_RUNTIME,
                "channel_prefix": settings.data_plane.events_channel.removesuffix(":events"),
            },
        )
    ]


def test_write_env_syncs_the_redis_url_with_the_active_passwords(tmp_path: Path) -> None:
    state = _state("runner")
    writes: list[dict[str, str]] = []

    def _upsert_env(_path: Path, values: dict[str, str], *, audit: object = None) -> None:
        writes.append(values)

    rotate.write_env(
        state,
        cfg=_gateway_cfg(),
        home_fn=lambda: tmp_path,
        upsert_fn=_upsert_env,
    )

    assert writes == [
        {
            "MEIVA_REDIS_ADMIN_PASSWORD": _OLD_REDIS_ADMIN,
            "MEIVA_REDIS_PASSWORD": _NEW_REDIS_RUNTIME,
            "MEIVA_REDIS_URL": writes[0]["MEIVA_REDIS_URL"],
        }
    ]
    assert urlsplit(writes[0]["MEIVA_REDIS_URL"]).password == _NEW_REDIS_RUNTIME


def test_preflight_and_verify_probe_the_state_host_as_the_url_user() -> None:
    state = _state()
    state.redis_host = "10.0.0.7"
    probes: list[tuple[str, str, str]] = []

    def _redis_probe(host: str, _port: int, password: str, *, username: str) -> bool:
        probes.append((host, username, password))
        return True

    assert rotate.preflight(state, probe_fn=_redis_probe) is True
    rotate.verify(state, probe_fn=_redis_probe)
    assert probes == [
        ("10.0.0.7", "default", _OLD_REDIS_ADMIN),
        ("10.0.0.7", "meiva", _OLD_REDIS_RUNTIME),
        ("10.0.0.7", "default", _NEW_REDIS_ADMIN),
        ("10.0.0.7", "meiva", _NEW_REDIS_RUNTIME),
    ]


def test_dry_run_never_calls_a_mutating_phase() -> None:
    state = _state()
    mutations: list[str] = []

    def _build_state(_scope: str, **_kwargs: object) -> rotate.RotationState:
        return state

    def _preflight(_state: rotate.RotationState) -> bool:
        return True

    def _apply_admin(_state: rotate.RotationState) -> None:
        mutations.append("admin")

    assert (
        rotate.main(
            [],
            cfg=_gateway_cfg(),
            build_state_fn=_build_state,
            preflight_fn=_preflight,
            apply_admin_fn=_apply_admin,
        )
        == 0
    )
    assert mutations == []


def test_recovery_state_is_owner_only(tmp_path: Path) -> None:
    state = _state()
    path = state.save(home=tmp_path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert rotate.RotationState.load(path) == state


def test_a_postgres_era_journal_is_refused_by_name(tmp_path: Path) -> None:
    data = {**asdict(_state()), "old_db_admin_password": "x", "pg_port": 5433}
    path = tmp_path / "data-plane-old.json"
    path.write_text(json.dumps(data))
    with pytest.raises(RuntimeError, match="not a Redis-only rotation journal"):
        rotate.RotationState.load(path)


def test_main_refuses_remote_managed_data_plane(capsys: pytest.CaptureFixture[str]) -> None:
    assert rotate.main(["--scope", "admin"], cfg=_gateway_cfg(is_remote=True)) == 1
    err = capsys.readouterr().err
    assert "remote-managed" in err
    assert "rotates credentials at the provider" in err
