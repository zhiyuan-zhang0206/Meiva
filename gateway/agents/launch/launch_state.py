# meiva:note
# What the gateway tells a spawner about a launch the runner did not acknowledge.
#
# The gateway commits the agent row first and then asks the runner to launch it. When that ask
# fails, the answer to the spawner is decided from the facts the shell read back: the row's
# status, whether the agent was admitted anyway, whether the attempt can still be retried. Values
# in, the verdict out; the reads and the writes stay in `gateway.agents.creation`.

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from base.agents.observation.evidence import AgentAvailability
from base.api_contracts.agent_contract import AgentLaunchFailed
from ops.rpc_schemas import ConfigNormalization, SpawnedAgent

__all__ = [
    "STORE_UNREADABLE",
    "LaunchState",
    "accepted_launch",
    "launch_failed_error",
    "launch_state",
]


# meiva:note
# The launch facts a failed dispatch is reported with.
@dataclass(frozen=True)
class LaunchState:
    view: dict[str, object]
    admitted: bool
    retry_legal: bool


# meiva:note
# The store could not be read or written after the failure: nothing is known, so a retry stays
# legal and the retry endpoint re-reads the row once Postgres returns.
STORE_UNREADABLE = LaunchState(
    {"status": "unknown", "availability": None}, admitted=False, retry_legal=True
)


# meiva:note
# The launch facts from the agent row (`status` is None when the row or its snapshot is gone).
def launch_state(
    status: str | None,
    admitted_at: datetime | None,
    attempt_id: object | None,
    availability: AgentAvailability | None,
) -> LaunchState:
    if status is None:
        return LaunchState(
            {"status": "unknown", "availability": None}, admitted=False, retry_legal=False
        )
    return LaunchState(
        view={
            "status": status,
            "availability": None if availability is None else availability.model_dump(mode="json"),
        },
        admitted=admitted_at is not None and status != "terminated",
        retry_legal=status == "idling" and admitted_at is None and attempt_id is not None,
    )


# meiva:note
# The 502 for a committed agent whose launch dispatch failed.
def launch_failed_error(
    agent_id: int, target: str, detail: str, state: LaunchState
) -> AgentLaunchFailed:
    return AgentLaunchFailed(
        f"Agent #{agent_id} was created; launch dispatch to {target} failed: {detail}",
        agent_id=agent_id,
        state=state.view,
        retry_launch_path=(
            f"/api/keyed/v1/agents/{agent_id}/retry-launch" if state.retry_legal else None
        ),
    )


# meiva:note
# The receipt of an accepted launch: acceptance, what was observed, any model rewrite.
def accepted_launch(
    spawned: SpawnedAgent,
    availability: AgentAvailability,
    normalization: tuple[str, str] | None,
) -> SpawnedAgent:
    receipt = spawned.model_copy(
        update={
            "accepted": True,
            "execution_observed": False,
            "reason": availability.reason,
            "observed_at": availability.observed_at,
        }
    )
    if normalization is None:
        return receipt
    return receipt.model_copy(
        update={
            "config_normalized": ConfigNormalization(
                requested=normalization[0], resolved=normalization[1]
            )
        }
    )
