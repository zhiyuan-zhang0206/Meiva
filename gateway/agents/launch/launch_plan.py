# meiva:note
# What the gateway asks a runner to launch and how it reads the answer: values in, decisions out.
#
# `gateway.agents.launch.forward` owns the effects (the dispatch over the wire). Every decision that
# needs no effect lives here, so a test passes values and asserts the returned refusal, payload
# or error:
#
# - which refusal a spawn target earns from its registry row,
# - the `spawn-launch` payload a committed launch becomes,
# - the typed error an unreachable, rejecting or malformed runner turns into,
# - how the launch state records a dispatch that raised.

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass

from pydantic import ValidationError

from base.agents.observation.evidence import AvailabilityReason
from base.api_contracts.agent_contract import (
    MachinePaused,
    MeivaAgentError,
    SpawnTargetNotAgentRunner,
)
from ops.rpc_schemas import LaunchAgentRequest, OpFailure, SpawnedAgent

__all__ = [
    "LaunchFailure",
    "LaunchForwardError",
    "launch_failure",
    "launch_op_payload",
    "launch_receipt",
    "launch_rejection",
    "launch_unreachable",
    "spawn_target_refusal",
]


# meiva:note
# Typed outcome of forwarding an already-committed agent launch.
class LaunchForwardError(Exception):
    def __init__(self, reason: AvailabilityReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


# meiva:note
# Why a committed launch did not reach a runner, as the launch state records it.
@dataclass(frozen=True)
class LaunchFailure:
    reason: AvailabilityReason
    detail: str


# meiva:note
# The error a spawn target earns from its registry row, or None when it may run agents.
def spawn_target_refusal(
    target: str, roles: Collection[str], *, paused: bool
) -> MeivaAgentError | None:
    if "agent-runner" not in roles:
        return SpawnTargetNotAgentRunner(
            f"spawn target {target!r} has no agent-runner capability; agents run on "
            "agent-runner machines. Pass body.machine = <an agent-runner name> "
            "(see meiva.agents.list_machines())."
        )
    if paused:
        return MachinePaused(
            f"spawn target {target!r} is paused (temporarily removed from the "
            "cluster); resume it first with `meiva cluster resume <name>` on the "
            "gateway, then spawn."
        )
    return None


# meiva:note
# The `spawn-launch` op payload for a committed launch.
def launch_op_payload(request: LaunchAgentRequest) -> dict[str, object]:
    return request.model_dump(mode="json", exclude_none=True)


def launch_unreachable(target: str, cause: str) -> LaunchForwardError:
    return LaunchForwardError(
        AvailabilityReason.LAUNCH_UNREACHABLE,
        f"target machine={target!r} ops server unreachable for spawn op: {cause}",
    )


# meiva:note
# The error for a runner that answered 'failed' to a launch.
def launch_rejection(target: str, result: Mapping[str, object]) -> LaunchForwardError:
    try:
        failure = OpFailure.model_validate(result)
    except ValidationError:
        return LaunchForwardError(
            AvailabilityReason.LAUNCH_UNREACHABLE,
            f"target machine={target!r} returned a malformed launch failure",
        )
    return LaunchForwardError(
        AvailabilityReason.LAUNCH_REJECTED,
        f"target machine={target!r} rejected launch: "
        f"{failure.reason or failure.error}: {failure.detail or failure.error}",
    )


# meiva:note
# The receipt a runner's answer carries, or the error for a malformed one.
#
# A runner may answer only for the agent it was asked to launch.
def launch_receipt(
    target: str, result: Mapping[str, object], agent_id: int
) -> SpawnedAgent | LaunchForwardError:
    try:
        spawned = SpawnedAgent.model_validate(result)
    except ValidationError:
        return LaunchForwardError(
            AvailabilityReason.LAUNCH_UNREACHABLE,
            f"target machine={target!r} returned a malformed launch receipt",
        )
    if spawned.id != agent_id:
        return LaunchForwardError(
            AvailabilityReason.LAUNCH_UNREACHABLE,
            f"target machine={target!r} returned agent {spawned.id} for {agent_id}",
        )
    return spawned


# meiva:note
# How the launch state records a launch dispatch that raised `error`.
def launch_failure(error: Exception) -> LaunchFailure:
    if isinstance(error, LaunchForwardError):
        return LaunchFailure(error.reason, str(error))
    return LaunchFailure(AvailabilityReason.LAUNCH_UNKNOWN, type(error).__name__)
