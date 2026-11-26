# meiva:link base/host/env/docs/bootstrap-mirror.md

import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest

from base.host.atomic_io import SYSTEM_FS, AtomicWriter
from base.host.env import bootstrap
from base.host.env.boot_policy import BootstrapFetch, issue_config_mirror, validate_config_mirror
from base.host.env.bootstrap import read_config_mirror, stage_config_mirror
from base.host.env.config_lite_table import BUILTIN_CLUSTER_CONFIG_FIELDS
from base.host.env.registry import PLUGIN_CLUSTER_CONFIG_ENV
from base.host.net.tests.local_http import closed_port_url, serve_json

_NOW = 1_000_000.0
_HOST = "runner.example"
_TOKEN = "runner-mirror-test"  # noqa: S105
_TUNING = BootstrapFetch(attempts=1)


def _values(db_url: str = "postgresql://gateway/db") -> dict[str, str]:
    return {
        "MEIVA_DB_URL": db_url,
        PLUGIN_CLUSTER_CONFIG_ENV: json.dumps(
            {
                plugin: dict.fromkeys(fields, None)
                for plugin, fields in BUILTIN_CLUSTER_CONFIG_FIELDS.items()
            }
        ),
    }


def test_content_generation_stays_stable_across_renewals() -> None:
    values = _values()
    original = issue_config_mirror(values, bearer=_TOKEN, machine_host=_HOST, now=_NOW)
    renewal = issue_config_mirror(
        dict(reversed(list(values.items()))), bearer=_TOKEN, machine_host=_HOST, now=_NOW + 100
    )
    changed = issue_config_mirror(
        _values("postgresql://gateway/new"), bearer=_TOKEN, machine_host=_HOST, now=_NOW + 100
    )
    assert original["generation"] == renewal["generation"] != changed["generation"]
    assert renewal["expires_at"] > original["expires_at"]
    assert renewal["integrity"] != original["integrity"]
    assert (
        validate_config_mirror(renewal, bearer=_TOKEN, machine_host=_HOST, now=_NOW + 100)
        == renewal
    )


@pytest.mark.parametrize(
    ("fault", "state"),
    [
        ("corrupt_json", "invalid"),
        ("other_host", "invalid"),
        ("other_role", "invalid"),
        ("tampered_values", "invalid"),
        ("wrong_token", "invalid"),
        ("public_file", "invalid"),
        ("expired", "expired"),
    ],
)
def test_offline_rejects_unusable_mirror_even_with_fresh_legacy_snapshot(
    tmp_path: Path, fault: str, state: Literal["invalid", "expired"]
) -> None:
    base_url = closed_port_url()
    validity = issue_config_mirror(_values(), bearer=_TOKEN, machine_host=_HOST, now=_NOW)
    now = float(validity["expires_at"]) if fault == "expired" else _NOW
    legacy = bootstrap.snapshot_path(tmp_path)
    legacy.parent.mkdir()
    _ = legacy.write_text(
        json.dumps(
            {
                "v": bootstrap.SNAPSHOT_VERSION,
                "base_url": base_url,
                "written_at": now,
                "values": _values(),
            }
        )
    )
    legacy.chmod(0o600)
    admitted = stage_config_mirror(
        validity, home=tmp_path, bearer=_TOKEN, machine_host=_HOST, now=_NOW
    )
    mirror = legacy.with_name("bootstrap-mirror.json")
    if fault == "corrupt_json":
        _ = mirror.write_text("{")
    elif fault == "other_role":
        _ = mirror.write_text(json.dumps(dict(admitted, role="gateway")))
    elif fault == "tampered_values":
        _ = mirror.write_text(
            json.dumps(dict(admitted, values=_values("postgresql://tampered/db")))
        )
    elif fault == "public_file":
        mirror.chmod(0o644)
    env = {
        "MEIVA_GATEWAY_URL": base_url,
        "MEIVA_API_TOKEN": "rotated-token" if fault == "wrong_token" else _TOKEN,
        "MEIVA_MACHINE_HOST": "other.example" if fault == "other_host" else _HOST,
    }

    with pytest.raises(bootstrap.BootstrapFetchError) as problem:
        _ = bootstrap.inject_config_from_gateway(env, home=tmp_path, now=now, tuning=_TUNING)
    assert problem.value.mirror_state == state
    assert "MEIVA_DB_URL" not in env


def test_flat_gateway_response_cannot_replace_an_existing_mirror(tmp_path: Path) -> None:
    payload = issue_config_mirror(_values(), bearer=_TOKEN, machine_host=_HOST, now=_NOW)
    _ = stage_config_mirror(payload, home=tmp_path, bearer=_TOKEN, machine_host=_HOST, now=_NOW)
    now = _NOW + _TUNING.snapshot_fresh_s + 1
    with serve_json(_values()) as gateway:
        env = {
            "MEIVA_GATEWAY_URL": gateway.url,
            "MEIVA_MACHINE_HOST": _HOST,
            "MEIVA_API_TOKEN": _TOKEN,
        }
        with pytest.raises(bootstrap.BootstrapFetchError, match="unsigned response"):
            _ = bootstrap.resolve_bootstrap_config(env, home=tmp_path, now=now, tuning=_TUNING)
    assert read_config_mirror(home=tmp_path, bearer=_TOKEN, machine_host=_HOST, now=now) == payload


def test_pre_stage_failed_rename_keeps_the_previous_mirror(tmp_path: Path) -> None:
    def refuse_replace(_source: Path, _target: Path) -> None:
        raise OSError("replace failed")

    original = issue_config_mirror(_values(), bearer=_TOKEN, machine_host=_HOST, now=_NOW)
    _ = stage_config_mirror(original, home=tmp_path, bearer=_TOKEN, machine_host=_HOST, now=_NOW)
    renewal = issue_config_mirror(
        _values("postgresql://gateway/new"), bearer=_TOKEN, machine_host=_HOST, now=_NOW + 1
    )
    writer = AtomicWriter(replace(SYSTEM_FS, replace=refuse_replace))
    with pytest.raises(OSError, match="replace failed"):
        _ = stage_config_mirror(
            renewal, home=tmp_path, bearer=_TOKEN, machine_host=_HOST, now=_NOW + 1, writer=writer
        )
    assert (
        read_config_mirror(home=tmp_path, bearer=_TOKEN, machine_host=_HOST, now=_NOW + 1)
        == original
    )
    assert [path.name for path in (tmp_path / "run").iterdir()] == ["bootstrap-mirror.json"]


@pytest.mark.parametrize(("status", "uses_mirror"), [(503, True), (401, False), (403, False)])
def test_stale_but_authorized_mirror_during_refresh(
    tmp_path: Path, status: int, uses_mirror: bool
) -> None:
    payload = issue_config_mirror(_values(), bearer=_TOKEN, machine_host=_HOST, now=_NOW)
    _ = stage_config_mirror(payload, home=tmp_path, bearer=_TOKEN, machine_host=_HOST, now=_NOW)
    with serve_json({}, status=status) as gateway:
        env = {
            "MEIVA_GATEWAY_URL": gateway.url,
            "MEIVA_MACHINE_HOST": _HOST,
            "MEIVA_API_TOKEN": _TOKEN,
        }
        now = _NOW + _TUNING.snapshot_fresh_s + 1
        if uses_mirror:
            result = bootstrap.resolve_bootstrap_config(env, home=tmp_path, now=now, tuning=_TUNING)
            assert result.source == "authorized_mirror"
            assert result.generation == payload["generation"]
        else:
            with pytest.raises(bootstrap.BootstrapFetchError, match=f"HTTP {status}"):
                _ = bootstrap.resolve_bootstrap_config(env, home=tmp_path, now=now, tuning=_TUNING)
        assert len(gateway.requests) == 1


@pytest.mark.parametrize("online", [False, True], ids=["offline-v4", "online-flat"])
def test_removing_the_mirror_does_not_reenable_unsigned_config(
    tmp_path: Path, online: bool
) -> None:
    signed = issue_config_mirror(_values(), bearer=_TOKEN, machine_host=_HOST, now=_NOW)
    _ = stage_config_mirror(signed, home=tmp_path, bearer=_TOKEN, machine_host=_HOST, now=_NOW)
    (tmp_path / "run" / "bootstrap-mirror.json").unlink()
    with serve_json(_values()) as gateway:
        env = {
            "MEIVA_GATEWAY_URL": gateway.url if online else closed_port_url(),
            "MEIVA_MACHINE_HOST": _HOST,
            "MEIVA_API_TOKEN": _TOKEN,
        }
        _ = bootstrap.snapshot_path(tmp_path).write_text(
            json.dumps(
                {
                    "v": bootstrap.SNAPSHOT_VERSION,
                    "base_url": env["MEIVA_GATEWAY_URL"],
                    "written_at": _NOW,
                    "values": _values(),
                }
            )
        )
        with pytest.raises(bootstrap.BootstrapFetchError) as problem:
            _ = bootstrap.resolve_bootstrap_config(env, home=tmp_path, now=_NOW, tuning=_TUNING)
    assert problem.value.mirror_state == "invalid"
    assert "unsigned" in str(problem.value) or "missing" in str(problem.value)


@pytest.mark.parametrize("nested_carrier", [False, True], ids=["envelope", "plugin-projection"])
def test_gateway_recovers_a_deeply_nested_local_mirror(
    tmp_path: Path, nested_carrier: bool
) -> None:
    signed = issue_config_mirror(_values(), bearer=_TOKEN, machine_host=_HOST, now=_NOW)
    _ = stage_config_mirror(signed, home=tmp_path, bearer=_TOKEN, machine_host=_HOST, now=_NOW)
    depth = sys.getrecursionlimit() + 100
    nested_json = "[" * depth + "]" * depth
    bad_body = (
        json.dumps(dict(signed, values={PLUGIN_CLUSTER_CONFIG_ENV: nested_json}))
        if nested_carrier
        else nested_json
    )
    _ = (tmp_path / "run" / "bootstrap-mirror.json").write_text(bad_body)
    with serve_json(signed) as gateway:
        env = {
            "MEIVA_GATEWAY_URL": gateway.url,
            "MEIVA_MACHINE_HOST": _HOST,
            "MEIVA_API_TOKEN": _TOKEN,
        }
        result = bootstrap.resolve_bootstrap_config(env, home=tmp_path, now=_NOW, tuning=_TUNING)
    assert result.source == "live_bootstrap"
    assert read_config_mirror(home=tmp_path, bearer=_TOKEN, machine_host=_HOST, now=_NOW) == signed
