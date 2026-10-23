# meiva:note
# The roster's decisions as values in, plan out: who is dialed, how a snapshot reads.

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gateway.cluster.roster.liveness_snapshot import MAX_AGE_S, Snapshot
from gateway.cluster.roster.roster_plan import (
    SnapshotVerdict,
    StepKind,
    plan_roster,
    snapshot_verdict,
)
from gateway.cluster.roster.roster_rows import RosterRowSpec

_NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def _row(name: str, *roles: str) -> RosterRowSpec:
    return RosterRowSpec(
        name=name,
        gateway_url=f"http://{name}:8113",
        role=list(roles or ("agent-runner",)),
        up_since_at=_NOW,
        description=None,
        stopped_at=None,
    )


def _snap(
    *,
    age_s: float = 5.0,
    reachable: bool = True,
    failures: int = 0,
    status: dict[str, object] | None = None,
) -> Snapshot:
    observed = _NOW - timedelta(seconds=age_s)
    return Snapshot(
        observed_at=observed,
        reachable=reachable,
        consecutive_failures=failures,
        status=status,
        status_at=None if status is None else observed,
    )


def test_a_snapshot_is_fresh_for_three_pass_intervals() -> None:
    assert _snap(age_s=5).fresh(_NOW)
    assert _snap(age_s=MAX_AGE_S - 1).fresh(_NOW)
    assert not _snap(age_s=MAX_AGE_S + 5).fresh(_NOW)


def test_two_failed_passes_make_a_fresh_snapshot_known_down() -> None:
    assert not _snap(reachable=False, failures=1).known_down(_NOW)
    assert _snap(reachable=False, failures=2).known_down(_NOW)
    # meiva:note
    # A stale row is no evidence the host is down: the pass may simply not be running.
    assert not _snap(reachable=False, failures=9, age_s=3600).known_down(_NOW)


def test_the_local_pure_gateway_reads_itself_and_nothing_else_does() -> None:
    rows = [_row("gw", "gateway"), _row("runner-a"), _row("other-gw", "gateway")]

    steps = plan_roster(rows, "gw", {"runner-a": _snap(), "other-gw": _snap()}, now=_NOW)

    assert [(s.row.name, s.kind) for s in steps] == [
        ("gw", StepKind.LOCAL),
        ("runner-a", StepKind.SNAPSHOT),
        ("other-gw", StepKind.SNAPSHOT),
    ]


def test_the_local_machine_that_is_also_a_runner_is_dialed_like_any_runner() -> None:
    [step] = plan_roster([_row("box", "gateway", "agent-runner")], "box", {}, now=_NOW)

    assert step.kind is StepKind.DIAL


def test_only_a_fresh_snapshot_spares_the_dial() -> None:
    rows = [_row("fresh-one"), _row("stale-one"), _row("unknown-one")]
    snaps = {"fresh-one": _snap(), "stale-one": _snap(age_s=3600)}

    steps = plan_roster(rows, "gw", snaps, now=_NOW)

    assert [s.kind for s in steps] == [StepKind.SNAPSHOT, StepKind.DIAL, StepKind.DIAL]
    assert steps[0].snapshot is snaps["fresh-one"]
    assert steps[1].snapshot is None


def test_a_fresh_read_passes_no_snapshots_and_dials_every_runner() -> None:
    rows = [_row("a"), _row("b"), _row("gw", "gateway")]

    steps = plan_roster(rows, "gw", None, now=_NOW)

    assert [(s.row.name, s.kind) for s in steps] == [
        ("a", StepKind.DIAL),
        ("b", StepKind.DIAL),
        ("gw", StepKind.LOCAL),
    ]


@pytest.mark.parametrize(
    ("snapshot", "verdict"),
    [
        (_snap(status={"machine_name": "x"}), SnapshotVerdict.REPORTED),
        # meiva:note
        # One dropped probe keeps the last status on screen.
        (
            _snap(reachable=False, failures=1, status={"machine_name": "x"}),
            SnapshotVerdict.REPORTED,
        ),
        # meiva:note
        # Two consecutive failed passes read offline.
        (
            _snap(reachable=False, failures=2, status={"machine_name": "x"}),
            SnapshotVerdict.OFFLINE,
        ),
        # meiva:note
        # A failed pass with no earlier answer to show reads offline.
        (_snap(reachable=False, failures=1, status=None), SnapshotVerdict.OFFLINE),
        # meiva:note
        # A reachable answer that is not a ClusterStatus is online + unknown.
        (_snap(reachable=True, status=None), SnapshotVerdict.UNKNOWN),
    ],
    ids=["answered", "one-dropped", "two-dropped", "never-answered", "not-a-status"],
)
def test_a_snapshot_reads_as(snapshot: Snapshot, verdict: SnapshotVerdict) -> None:
    assert snapshot_verdict(snapshot) is verdict
