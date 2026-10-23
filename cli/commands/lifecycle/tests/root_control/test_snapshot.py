# meiva:note
# The status snapshot is checked once, at the boundary, into typed rows.

from __future__ import annotations

from typing import Any

import pytest

from cli.commands.lifecycle.root_control.snapshot import SnapshotError, parse_snapshot, root_units


def reply(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "root": {"pid": 90, "create_time": 12.5, "starttime": None, "launch_digest": "d" * 64},
        "units": [
            {
                "id": "gateway",
                "state": "running",
                "intent": "running",
                "manifest_digest": "m1",
                "last_error": None,
                "last_exit": "signal 9",
                "exec": ["/bin/sh"],
            },
            {"id": "frontend", "state": "stopped"},
        ],
        "health": {"gateway": {"restart_limit_reached": True, "breaker_open": True}},
    }
    body.update(overrides)
    return body


def test_a_valid_reply_becomes_typed_rows_without_the_fields_nobody_reads() -> None:
    snapshot = parse_snapshot(reply())

    assert snapshot["root"] == {
        "pid": 90,
        "create_time": 12.5,
        "starttime": None,
        "launch_digest": "d" * 64,
    }
    assert root_units(snapshot) == {
        "gateway": {
            "id": "gateway",
            "state": "running",
            "intent": "running",
            "manifest_digest": "m1",
            "last_exit": "signal 9",
        },
        "frontend": {"id": "frontend", "state": "stopped"},
    }
    assert snapshot.get("health") == {"gateway": {"restart_limit_reached": True}}


def test_units_keep_the_roots_declaration_order() -> None:
    body = reply(
        units=[{"id": name, "state": "running"} for name in ("agent-host", "gateway", "z")]
    )

    assert list(root_units(parse_snapshot(body))) == ["agent-host", "gateway", "z"]


def test_a_reply_without_health_or_a_launch_digest_is_still_a_snapshot() -> None:
    body = reply()
    del body["health"]
    del body["root"]["launch_digest"]

    snapshot = parse_snapshot(body)

    assert "health" not in snapshot
    assert snapshot["root"]["launch_digest"] is None


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"root": None}, "root block"),
        ({"root": {"pid": True, "create_time": 1.0, "starttime": None}}, "native identity"),
        ({"root": {"pid": 9, "create_time": "now", "starttime": None}}, "native identity"),
        ({"root": {"pid": 9, "create_time": 1.0, "starttime": "7"}}, "native identity"),
        ({"units": "gateway"}, "unit roster"),
        ({"units": [{"id": "gateway"}]}, "id or state"),
        ({"units": [{"id": "", "state": "running"}]}, "id or state"),
        ({"units": [{"id": "gateway", "state": "running", "intent": 3}]}, "non-text intent"),
        ({"health": {"gateway": {}}}, "restart_limit_reached"),
        ({"health": []}, "health block"),
    ],
)
def test_a_reply_lacking_a_field_the_commands_read_is_refused(
    override: dict[str, Any], message: str
) -> None:
    with pytest.raises(SnapshotError, match=message):
        parse_snapshot(reply(**override))


def test_a_result_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(SnapshotError, match="root status is not an object"):
        parse_snapshot(["units"])
