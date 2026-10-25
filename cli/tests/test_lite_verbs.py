from __future__ import annotations

import json
from pathlib import Path

import pytest

from base.cluster.machine import GatewayAccess, GatewayApiTokenMissing, gateway_auth_headers
from base.host.db_authority import unit
from base.host.net.auth import delivered_token
from base.host.private_storage import write_private_bytes
from base.native_process.root_control import instance_lock
from base.paths import repo_root, root_run_dir
from cli import main as cli_main

_EXPLICIT_BEARER = "explicit-operator-api-bearer-00000001"
_CAPABILITY_BEARER = "runner-capability-api-bearer-00000001"


def _dispatched_fetch_env(argv: list[str], *, home: Path) -> str | None:
    env: dict[str, str] = {}
    cli_main.opt_into_lite_config(argv, environ=env, home=home)
    return env.get("MEIVA_CONFIG_FETCH")


def _install_runner_capability(home: Path, *, token: str) -> None:
    resolved = home.resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    (resolved / ".env").write_text(
        "MEIVA_MACHINE_SERVE_AGENT_RUNNER=true\nMEIVA_MACHINE_SERVE_GATEWAY=false\n",
        encoding="utf-8",
    )
    write_private_bytes(
        resolved / "start-intent.json",
        json.dumps({"home": str(resolved), "checkout": str(repo_root().resolve())}).encode(),
    )
    capability = unit.UnitCapability(
        version=1,
        unit=unit.UnitIdentity(machine="runner-1", home=str(resolved)),
        endpoint="postgresql://meiva@10.0.0.7:6433/meiva",
        generation=unit.GenerationRef(number=1, credential_digest="a" * 64),
        role="meiva_runner",
        password="p" * 40,
        bundle="0" * 32,
        api=unit.UnitApi(token=token, gateway="b" * 64, telemetry="c" * 40),
    )
    write_private_bytes(
        unit.unit_capability_path(resolved),
        (capability.model_dump_json() + "\n").encode(),
    )


def test_restart_is_not_settings_lite(tmp_path: Path) -> None:
    assert _dispatched_fetch_env(["restart"], home=tmp_path) is None


def test_stop_of_a_running_root_requires_cluster_config(tmp_path: Path) -> None:
    lock = instance_lock.acquire_instance_lock(root_run_dir(tmp_path))
    try:
        assert _dispatched_fetch_env(["stop"], home=tmp_path) is None
    finally:
        instance_lock.release_instance_lock(lock)


def test_force_stop_stays_settings_lite(tmp_path: Path) -> None:
    assert _dispatched_fetch_env(["stop", "--force"], home=tmp_path) == "skip"


def test_stop_with_no_running_root_stays_settings_lite(tmp_path: Path) -> None:
    assert _dispatched_fetch_env(["stop"], home=tmp_path) == "skip"


def test_pty_stays_settings_lite(tmp_path: Path) -> None:
    assert _dispatched_fetch_env(["pty", "status"], home=tmp_path) == "skip"


def test_status_stays_settings_lite(tmp_path: Path) -> None:
    assert _dispatched_fetch_env(["status"], home=tmp_path) == "skip"


@pytest.mark.parametrize(
    ("explicit", "capability_bearer", "expected"),
    [
        (_EXPLICIT_BEARER, _CAPABILITY_BEARER, _EXPLICIT_BEARER),
        (None, _CAPABILITY_BEARER, _CAPABILITY_BEARER),
        (None, None, None),
    ],
)
def test_lite_cli_uses_its_local_runner_api_token(
    tmp_path: Path,
    explicit: str | None,
    capability_bearer: str | None,
    expected: str | None,
) -> None:
    if capability_bearer is not None:
        _install_runner_capability(tmp_path, token=capability_bearer)
    else:
        (tmp_path / ".env").write_text(
            "MEIVA_MACHINE_SERVE_AGENT_RUNNER=true\nMEIVA_MACHINE_SERVE_GATEWAY=false\n",
            encoding="utf-8",
        )
    env: dict[str, str] = {}
    if explicit is not None:
        env["MEIVA_API_TOKEN"] = explicit

    cli_main.opt_into_lite_config(["agents"], environ=env, home=tmp_path)
    try:
        headers = gateway_auth_headers(
            GatewayAccess(
                gateway_url="http://gw",
                cluster_secret="",
                remote_plane=False,
                machine_token=delivered_token(env),
                launcher_profile=None,
                gateway_home=False,
            )
        )
    except GatewayApiTokenMissing:
        headers = {}
    assert headers == ({} if expected is None else {"Authorization": f"Bearer {expected}"})


def test_lite_cli_in_agent_profile_does_not_consume_operator_capability(
    tmp_path: Path,
) -> None:
    _install_runner_capability(tmp_path, token=_CAPABILITY_BEARER)
    env: dict[str, str] = {"MEIVA_PROCESS_PROFILE": "agent"}
    cli_main.normalize_profile_env(env)
    cli_main.opt_into_lite_config(["agents"], environ=env, home=tmp_path)
    assert env.get("MEIVA_LAUNCHER_PROFILE") == "agent"
    assert "MEIVA_API_TOKEN" not in env
