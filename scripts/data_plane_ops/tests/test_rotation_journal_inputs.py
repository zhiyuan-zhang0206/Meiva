# meiva:note
# Real JSON files exercise the persisted boundary. Values are fixture-only strings;
# no test loads an operator journal or runs a live credential rotation.
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, fields
from pathlib import Path

import pytest

from base.host.proc import run_bounded
from scripts.data_plane_ops import rotate_data_plane_secrets as rotate

_SENTINEL = "journal-fixture-private-sentinel"
_TEXT_FIELDS = [item.name for item in fields(rotate.RotationState) if item.name != "redis_port"]


@pytest.fixture
def payload() -> dict[str, object]:
    return asdict(
        rotate.RotationState(
            scope=rotate.RotationScope.BOTH,
            old_redis_admin_password=_SENTINEL + "-old-admin",
            new_redis_admin_password=_SENTINEL + "-new-admin",
            old_redis_password=_SENTINEL + "-old-runner",
            new_redis_password=_SENTINEL + "-new-runner",
            redis_port=16380,
            redis_host="redis.example.test",
            redis_user="runtime-user",
            started_at="2026-10-25T01:02:03+00:00",
            phase=rotate.RotationPhase.MINTED,
        )
    )


def _write(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "fixture-journal.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _refused(path: Path) -> str:
    with pytest.raises(RuntimeError) as failure:
        rotate.RotationState.load(path)
    message = str(failure.value)
    assert _SENTINEL not in message
    return message


@pytest.mark.parametrize("scope", list(rotate.RotationScope))
def test_valid_scope_preserves_the_saved_values(
    tmp_path: Path, payload: dict[str, object], scope: rotate.RotationScope
) -> None:
    payload["scope"] = scope
    state = rotate.RotationState.load(_write(tmp_path, payload))
    assert asdict(state) == payload
    assert state.rotates_admin == (scope in {rotate.RotationScope.ADMIN, rotate.RotationScope.BOTH})
    assert state.rotates_runner == (
        scope in {rotate.RotationScope.RUNNER, rotate.RotationScope.BOTH}
    )
    assert _SENTINEL not in repr(state)


@pytest.mark.parametrize("phase", list(rotate.RotationPhase))
def test_every_saved_progress_marker_is_preserved_without_restarting_the_record(
    tmp_path: Path, payload: dict[str, object], phase: rotate.RotationPhase
) -> None:
    payload["phase"] = phase
    state = rotate.RotationState.load(_write(tmp_path, payload))
    assert state.phase == phase
    assert state.started_at == payload["started_at"]
    assert asdict(state) == payload


@pytest.mark.parametrize("name", [item.name for item in fields(rotate.RotationState)])
def test_resume_requires_every_saved_field(
    tmp_path: Path, payload: dict[str, object], name: str
) -> None:
    del payload[name]
    assert "not a Redis-only rotation journal" in _refused(_write(tmp_path, payload))


@pytest.mark.parametrize("name", _TEXT_FIELDS)
def test_every_text_field_refuses_a_non_string(
    tmp_path: Path, payload: dict[str, object], name: str
) -> None:
    payload[name] = {_SENTINEL: "not a string"}
    assert "field" in _refused(_write(tmp_path, payload))


@pytest.mark.parametrize("name", _TEXT_FIELDS)
def test_every_text_field_needs_a_value(
    tmp_path: Path, payload: dict[str, object], name: str
) -> None:
    payload[name] = ""
    assert "field" in _refused(_write(tmp_path, payload))


@pytest.mark.parametrize("value", [None, False, 1, [_SENTINEL]])
def test_json_primitives_are_not_coerced_to_passwords(
    tmp_path: Path, payload: dict[str, object], value: object
) -> None:
    payload["old_redis_password"] = value
    assert "old_redis_password" in _refused(_write(tmp_path, payload))


@pytest.mark.parametrize("value", [True, False, 0, -1, 65536, "16380", 16380.0, None])
def test_the_recorded_port_is_a_real_integer_tcp_port(
    tmp_path: Path, payload: dict[str, object], value: object
) -> None:
    payload["redis_port"] = value
    assert "redis_port" in _refused(_write(tmp_path, payload))


@pytest.mark.parametrize("port", [1, 65535])
def test_both_tcp_port_endpoints_remain_valid(
    tmp_path: Path, payload: dict[str, object], port: int
) -> None:
    payload["redis_port"] = port
    assert rotate.RotationState.load(_write(tmp_path, payload)).redis_port == port


@pytest.mark.parametrize("name", ["scope", "phase"])
def test_unknown_vocabulary_is_refused_without_echoing_the_value(
    tmp_path: Path, payload: dict[str, object], name: str
) -> None:
    payload[name] = _SENTINEL
    assert name in _refused(_write(tmp_path, payload))


@pytest.mark.parametrize("value", [None, []])
def test_a_journal_is_an_object(tmp_path: Path, value: object) -> None:
    assert "not a Redis-only rotation journal" in _refused(_write(tmp_path, value))


def test_unknown_fields_are_refused_without_echoing_the_key(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    payload[_SENTINEL] = _SENTINEL
    assert "not a Redis-only rotation journal" in _refused(_write(tmp_path, payload))


@pytest.mark.parametrize("value", ["nested/../../../elsewhere", "bad\0stamp"])
def test_resume_time_cannot_escape_its_filename_component(
    tmp_path: Path, payload: dict[str, object], value: str
) -> None:
    payload["started_at"] = value
    assert "started_at" in _refused(_write(tmp_path, payload))


@pytest.mark.parametrize("raw", [b'{"scope":"journal-fixture-private-sentinel', b"\xff"])
def test_corrupt_json_has_a_value_free_diagnostic(tmp_path: Path, raw: bytes) -> None:
    path = tmp_path / "fixture-journal.json"
    path.write_bytes(raw)
    assert _refused(path) == "invalid Redis rotation journal JSON"


def test_password_and_endpoint_bytes_are_not_normalized(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    payload["old_redis_password"] = f"  {_SENTINEL}  "
    payload.update(
        redis_host="host-without-a-policy",
        redis_user="runtime user",
        started_at="older-flat-component",
    )
    state = rotate.RotationState.load(_write(tmp_path, payload))
    assert asdict(state) == payload


@pytest.mark.parametrize("name", ["scope", "phase", "redis_port", "old_redis_password"])
def test_real_cli_refuses_bad_resume_before_any_network_or_write(
    tmp_path: Path, payload: dict[str, object], name: str
) -> None:
    payload[name] = _SENTINEL if name in {"scope", "phase"} else True
    path = _write(tmp_path, payload)
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("# fixture-only home\n", encoding="utf-8")
    result = run_bounded(
        [sys.executable, "-m", "scripts.data_plane_ops.tests.rotation_journal_child", str(path)],
        timeout=15,
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        env={
            "PATH": os.defpath,
            "HOME": str(tmp_path),
            "MEIVA_HOME": str(home),
            "MEIVA_CONFIG_FETCH": "skip",
            "MEIVA_CONFIG_BOOT": "eager",
            "MEIVA_OS_JOBS_ENABLED": "false",
            "MEIVA_DB_URL": "postgresql://fixture@127.0.0.1:1/fixture",
            "MEIVA_REDIS_URL": "redis://fixture:fixture@127.0.0.1:1/0",
        },
    )
    assert result.returncode == 0, result.stderr
    assert _SENTINEL not in result.stdout + result.stderr
    observed = json.loads(result.stdout)
    assert observed["refused"] is not None and name in observed["refused"]
    assert observed["returned"] is None
    assert observed["attempted"] == []
    assert observed["home_unchanged"] is True
