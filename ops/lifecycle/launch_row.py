# meiva:note
# Whether an agent's stored row still matches the launch attempt a gateway asks the runner to wake.
#
# A stale attempt cannot wake an identity now placed somewhere else, and a row born on a harness
# with no launcher is refused rather than quietly run under the built-in loop's label. The shell
# (`launch.validate_launch_row`) reads the row and passes the harnesses it can launch; the
# refusal is a `ValueError` raised here, before anything is woken.

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from uuid import UUID

from base.api_contracts.agent_contract import AgentStatus, Harness

__all__ = ["StoredLaunch", "require_current_launch_row"]


# meiva:note
# The columns of `agents_meta` a launch is judged against.
@dataclass(frozen=True)
class StoredLaunch:
    machine: str | None
    last_launch_attempt_id: UUID | None
    status: str
    harness: str


# meiva:note
# Refuse a row that is missing, placed elsewhere, past this attempt, admitted, or on a harness
# with no launcher.
def require_current_launch_row(
    row: StoredLaunch | None,
    *,
    agent_id: int,
    launch_attempt_id: UUID,
    local_machine: str,
    launchable: Collection[Harness],
) -> Harness:
    if row is None or (row.machine, row.last_launch_attempt_id, row.status) != (
        local_machine,
        launch_attempt_id,
        AgentStatus.IDLING,
    ):
        raise ValueError(f"agent {agent_id} launch attempt is stale or misplaced")
    # meiva:note
    # A row records the harness it was born on; refusing to wake one whose
    # launcher does not exist keeps that record honest instead of quietly
    # running the built-in loop under an external harness label.
    # meiva:link base/agents/docs/harness/harness.meiva.okf.md
    harness = Harness(row.harness)
    if harness not in launchable:
        raise ValueError(
            f"agent {agent_id} was born on harness {harness.value!r}, which has no "
            f"launcher yet; only {sorted(item.value for item in launchable)} can "
            f"start a session today"
        )
    return harness
