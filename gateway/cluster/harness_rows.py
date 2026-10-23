# meiva:note
# What a machine's harness probe says, as the spawn form's rows.
#
# `launchable` joins two separate facts and must not report either alone: a launcher
# for the harness exists in this build (the caller's `launchable_harnesses`, which
# `base.agents.birth.harness.LAUNCHABLE_HARNESSES` owns) AND the machine can actually run
# it — installed and signed in, or the built-in runtime, which needs neither. Testing only the
# set would offer a choice the spawn then refuses.

from __future__ import annotations

from collections.abc import Collection

from base.api_contracts.agent_contract import HARNESS_BILLING, Harness
from gateway.cluster.schemas import HarnessAvailability
from ops.rpc_schemas import HarnessProbeResult, HarnessProbeRow

__all__ = ["availability_rows", "launchable"]


# meiva:link base/agents/docs/harness/harness.meiva.okf.md
def launchable(row: HarnessProbeRow, launchable_harnesses: Collection[Harness]) -> bool:
    if row.harness not in launchable_harnesses:
        return False
    return row.harness is Harness.MEIVA or row.status == "available"


def availability_rows(
    probed: HarnessProbeResult, launchable_harnesses: Collection[Harness]
) -> list[HarnessAvailability]:
    return [
        HarnessAvailability(
            harness=row.harness,
            status=row.status,
            reason=row.reason,
            launchable=launchable(row, launchable_harnesses),
            billing=HARNESS_BILLING[row.harness],
        )
        for row in probed.harnesses
    ]
