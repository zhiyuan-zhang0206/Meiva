# meiva:note
# What the gateway asks a home runner for a lifecycle request and how it reads a failure.
#
# `gateway.agents.launch.forward` owns the effects (the dispatch and the clock bound around it). The
# op a request becomes and the typed error a failed, unreachable or silent runner stands for are
# pure functions here: a test passes values and asserts what comes back.

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import ValidationError

from base.api_contracts.agent_contract import (
    EXCEPTION_BY_REASON,
    CrossMachineGatewayUnavailable,
    ErrorReason,
    MeivaAgentError,
)
from ops.rpc_schemas import OpFailure

__all__ = [
    "LifecycleOp",
    "ProxiedFailure",
    "lifecycle_op",
    "lifecycle_timeout",
    "lifecycle_unreachable",
    "proxied_failure",
]


# meiva:note
# One lifecycle request as the op sent to the home runner.
@dataclass(frozen=True)
class LifecycleOp:
    kind: str
    payload: dict[str, object]
    timeout_s: float
    retries: int
    idempotency_key: str | None


# meiva:note
# The error a runner's failed op stands for.
#
# `unknown_reason` is a wire reason this gateway has no exception for; the op's error text
# then travels as a generic unavailable error.
@dataclass(frozen=True)
class ProxiedFailure:
    error: MeivaAgentError
    unknown_reason: str | None


# meiva:note
# The 'lifecycle' op that carries a request to the agent's home runner.
def lifecycle_op(
    path: str,
    json_body: Mapping[str, object],
    *,
    timeout_s: float,
    retries: int,
    idempotency_key: str | None,
) -> LifecycleOp:
    return LifecycleOp(
        kind="lifecycle",
        payload={"path": path, "body": dict(json_body)},
        timeout_s=timeout_s,
        retries=retries,
        idempotency_key=idempotency_key,
    )


def lifecycle_timeout(target: str, deadline_s: float) -> CrossMachineGatewayUnavailable:
    return CrossMachineGatewayUnavailable(
        f"target machine={target!r} lifecycle op did not answer within {deadline_s:.0f}s"
    )


def lifecycle_unreachable(target: str, cause: str) -> CrossMachineGatewayUnavailable:
    return CrossMachineGatewayUnavailable(
        f"target machine={target!r} ops server unreachable for lifecycle op: {cause}"
    )


# meiva:note
# Rebuild the error a failed `/ops` result stands for.
#
# A result with a recognized wire `reason` becomes the matching `MeivaAgentError`, so the
# original semantics cross the machine boundary unchanged. Anything else, a payload that is
# not even an `OpFailure` included, becomes `CrossMachineGatewayUnavailable` carrying it.
def proxied_failure(payload: Mapping[str, object]) -> ProxiedFailure:
    generic = CrossMachineGatewayUnavailable(
        f"target machine reported failure via queue: {dict(payload)!r}"
    )
    try:
        failure = OpFailure.model_validate(payload)
    except ValidationError:
        return ProxiedFailure(generic, None)
    if failure.reason is None or failure.detail is None:
        return ProxiedFailure(generic, None)
    try:
        reason = ErrorReason(failure.reason)
    except ValueError:
        return ProxiedFailure(generic, failure.reason)
    return ProxiedFailure(EXCEPTION_BY_REASON[reason](failure.detail), None)
