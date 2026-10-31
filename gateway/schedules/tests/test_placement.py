from __future__ import annotations

from gateway.schedules.placement import MachineSnapshot, judge_placement


def test_judge_placement_verdicts() -> None:
    # 1. Unregistered machine
    v = judge_placement(
        machine="unknown-box",
        snapshot=MachineSnapshot(
            registered=False,
            paused_at=None,
            pause_reason=None,
            stopped_at=None,
            maintenance_reason=None,
            online=None,
            consecutive_failures=0,
        ),
    )
    assert v.status == "unregistered"
    assert v.reason == "machine 'unknown-box' is not registered in the cluster"

    # 2. Paused machine
    v = judge_placement(
        machine="box-1",
        snapshot=MachineSnapshot(
            registered=True,
            paused_at="2026-10-31T12:00:00Z",  # type: ignore[arg-type]
            pause_reason="operator upgrade",
            stopped_at=None,
            maintenance_reason=None,
            online=True,
            consecutive_failures=0,
        ),
    )
    assert v.status == "paused"
    assert v.reason == "operator upgrade"

    # 3. Stopped machine
    v = judge_placement(
        machine="box-2",
        snapshot=MachineSnapshot(
            registered=True,
            paused_at=None,
            pause_reason=None,
            stopped_at="2026-10-31T12:00:00Z",  # type: ignore[arg-type]
            maintenance_reason="hardware maintenance",
            online=True,
            consecutive_failures=0,
        ),
    )
    assert v.status == "stopped"
    assert v.reason == "hardware maintenance"

    # 4. Offline machine
    v = judge_placement(
        machine="box-3",
        snapshot=MachineSnapshot(
            registered=True,
            paused_at=None,
            pause_reason=None,
            stopped_at=None,
            maintenance_reason=None,
            online=False,
            consecutive_failures=3,
        ),
    )
    assert v.status == "offline"
    assert v.reason == "machine 'box-3' is offline (3 failed probes)"

    # 5. Healthy online machine
    v = judge_placement(
        machine="box-4",
        snapshot=MachineSnapshot(
            registered=True,
            paused_at=None,
            pause_reason=None,
            stopped_at=None,
            maintenance_reason=None,
            online=True,
            consecutive_failures=0,
        ),
    )
    assert v.status == "ok"
    assert v.reason is None
