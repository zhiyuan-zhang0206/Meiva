from __future__ import annotations

import json
import os

import pytest

from base.native_process.root_control import client
from base.native_process.root_control.ipc import ProtocolError, parse_request


def _caller(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "pid": 10,
        "ppid": 9,
        "uid": 501,
        "cwd": "/srv/meiva",
        "argv": ["meiva", "stop"],
        "agent_id": None,
        "machine": "macmini",
        "operation_id": None,
        "env_keys": [],
    }
    base.update(overrides)
    return base


# meiva:note
# A state-changing request carries who asked; a verb that changes nothing
# rejects the block instead of silently dropping it.
# meiva:link base/native_process/root_control/docs/lifecycle-caller.meiva.okf.md
def test_a_down_request_carries_its_caller() -> None:
    request = parse_request(
        json.dumps({"verb": "down", "name": "gateway", "caller": _caller()}).encode()
    )

    assert request["caller"] == _caller()
    assert request["name"] == "gateway"


def test_a_status_request_takes_no_caller() -> None:
    with pytest.raises(ProtocolError):
        parse_request(json.dumps({"verb": "status", "caller": _caller()}).encode())


def test_a_caller_with_an_unknown_field_is_rejected() -> None:
    with pytest.raises(ProtocolError):
        parse_request(
            json.dumps({"verb": "down", "name": "x", "caller": _caller(extra=1)}).encode()
        )


def test_redaction_drops_sensitive_values_and_keeps_flags() -> None:
    argv = client.redacted_argv(
        ["meiva", "restart", "--token", "abc123", "--secret=shh", "-p", "hunter2", "--name", "gw"]
    )

    assert argv == [
        "meiva",
        "restart",
        "--token",
        "<redacted>",
        "--secret=<redacted>",
        "-p",
        "<redacted>",
        "--name",
        "gw",
    ]


def test_identity_records_names_not_values() -> None:
    names = client.sensitive_env_keys(
        {"SOME_TOKEN": "value-that-must-not-leak", "PLAIN_SETTING": "visible"}
    )

    assert names == ["SOME_TOKEN"]
    identity = client.caller_identity()
    assert "value-that-must-not-leak" not in json.dumps(identity)
    assert identity["pid"] == os.getpid()
