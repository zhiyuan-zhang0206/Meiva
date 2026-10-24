# meiva:note
# The `meiva cluster status` roster table, decided from values.
#
# Rows are built from the real `MachineStatus` wire schema, so a renamed or dropped field the
# renderer reads fails here instead of drifting silently (the KeyError('role') crash shipped
# that way when the fixtures carried a `role` field the schema no longer had).

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from base.api_contracts.status import SchemaMismatchStatus
from cli.commands.cluster.plans.roster_table import (
    code_cell,
    roster_lines,
    schema_mismatch_banner,
    status_cell,
)
from cli.commands.cluster.tests.roster_rows import machine

_STOPPED = datetime(2026, 6, 1, 6, 0, tzinfo=UTC)


def _line(lines: tuple[str, ...], name: str) -> str:
    return next(line for line in lines if line.startswith(name))


def test_the_running_commit_is_shown_short_when_it_is_the_checkouts() -> None:
    assert code_cell(running_sha="abc1234def", head_sha="abc1234def") == "abc1234"


def test_a_process_on_another_commit_than_its_checkout_is_marked_stale() -> None:
    assert code_cell(running_sha="999888777", head_sha="abc1234def") == "⚠ 9998887"


def test_a_process_that_froze_no_commit_reads_a_dash() -> None:
    assert code_cell(running_sha=None, head_sha="abc1234def") == "—"


@pytest.mark.parametrize(
    ("online", "mismatch", "stopped_at", "cell"),
    [
        (True, True, None, "MISMATCH"),
        (True, True, _STOPPED, "MISMATCH"),
        (True, False, None, "online"),
        (False, False, _STOPPED, "stopped"),
        (False, False, None, "offline"),
        (True, False, _STOPPED, "STALE-STOP"),
    ],
)
def test_the_status_cell_names_a_contradiction_rather_than_picking_a_side(
    online: bool, mismatch: bool, stopped_at: datetime | None, cell: str
) -> None:
    assert status_cell(online=online, identity_mismatch=mismatch, stopped_at=stopped_at) == cell


def test_the_table_has_a_code_column_and_no_pin_verdict() -> None:
    lines = roster_lines(
        [
            machine(name="cloud", head_sha="abc1234def", running_sha="abc1234def"),
            machine(
                name="runner-a",
                serve_gateway=False,
                head_sha="999888777",
                running_sha="999888777",
            ),
        ]
    )

    header = lines[0].split()
    assert "code" in header and "pin" not in header
    assert "✓" not in "\n".join(lines) and "✗" not in "\n".join(lines)
    assert "gateway + agent-runner" in _line(lines, "cloud")
    runner = _line(lines, "runner-a")
    assert "agent-runner" in runner and "gateway +" not in runner


def test_the_role_column_names_the_observability_station() -> None:
    lines = roster_lines(
        [
            machine(name="station-a", serve_gateway=False, serve_agent_runner=False).model_copy(
                update={"serve_observability_station": True}
            ),
            machine(name="combo", serve_agent_runner=False).model_copy(
                update={"serve_observability_station": True}
            ),
            machine(name="runner-a", serve_gateway=False),
        ]
    )

    station = _line(lines, "station-a")
    assert "observability-station" in station and "gateway" not in station
    assert "gateway + observability-station" in _line(lines, "combo")
    assert "observability-station" not in _line(lines, "runner-a")


def test_a_row_flagged_as_the_wrong_host_and_a_row_on_stale_code_are_loud() -> None:
    lines = roster_lines(
        [
            machine(name="impostor", online=False, identity_mismatch=True),
            machine(name="stale", head_sha="abc1234def", running_sha="999888777"),
        ]
    )

    assert "MISMATCH" in _line(lines, "impostor")
    assert "⚠ 9998887" in _line(lines, "stale")


def test_the_status_column_tells_online_stopped_and_offline_apart() -> None:
    lines = roster_lines(
        [
            machine(name="up", stopped_at=None),
            machine(name="stopped-host", online=False, paused=None, stopped_at=_STOPPED),
            machine(name="gone", online=False, paused=None, stopped_at=None),
        ]
    )

    up = _line(lines, "up")
    assert " no " in up and " online " in up
    assert "stopped" in _line(lines, "stopped-host") and "?" in _line(lines, "stopped-host")
    assert "offline" in _line(lines, "gone")


def test_a_staging_host_carries_the_mark_and_widens_the_name_column() -> None:
    lines = roster_lines([machine(name="canary", is_staging=True), machine(name="b")])

    assert _line(lines, "canary (staging)")
    assert lines[1] == "-" * (len("canary (staging)") + 71)
    assert lines[0].startswith("name".ljust(len("canary (staging)")) + "  role")


def test_an_offline_host_has_no_up_since() -> None:
    lines = roster_lines([machine(name="gone", online=False, paused=None)])

    assert _line(lines, "gone").endswith(" —")
    assert "2026-06-01" not in _line(lines, "gone")


def test_a_schema_disagreement_is_shown_above_the_table_even_while_ops_is_online() -> None:
    mismatch = SchemaMismatchStatus(
        kind="divergent", machine="runner-a", detail="applied {old} vs required {new}"
    )

    lines = roster_lines([machine(name="runner-a", schema_mismatch=mismatch)])

    assert lines[0] == "⚠ schema check on runner-a: divergent; applied {old} vs required {new}"
    assert lines[1].startswith("name")
    assert schema_mismatch_banner([machine()]) == ()
