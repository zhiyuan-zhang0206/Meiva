# meiva:note
# Rollout robustness — a defect a live 2026-07-28 rollout exposed.
#
# The roster showed a host `online` throughout while its fan-out had excluded it for a stale
# `machines.stopped_at`, so the two sources of truth disagreed with nothing to see. (The other
# defects of that rollout, a declined restart and a failed restart, are asserted on the restart
# plan: `stop/test_restart_plan.py`.)

from __future__ import annotations

from datetime import UTC, datetime

# ─── Defect 1 (visibility): the roster names the disagreement ────────────────


def test_roster_flags_a_live_host_that_carries_a_stop_marker() -> None:
    """`online` here is what hid the exclusion: the roster's own source of truth
    (a live probe) contradicted the fan-out's (the marker) with nothing to see."""
    from cli.commands.cluster.control import status_cell

    stopped = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    assert status_cell(online=True, identity_mismatch=False, stopped_at=stopped) == "STALE-STOP"
    # the other three verdicts are unchanged
    assert status_cell(online=True, identity_mismatch=False, stopped_at=None) == "online"
    assert status_cell(online=False, identity_mismatch=False, stopped_at=stopped) == "stopped"
    assert status_cell(online=True, identity_mismatch=True, stopped_at=stopped) == "MISMATCH"
