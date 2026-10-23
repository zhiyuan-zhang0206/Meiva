# meiva:note
# What a `PUT /api/config` may write and how a failed write is reported: values in, decisions out.
#
# `runtime.py` owns the effects (the candidate validation that reads `.env`, the host op, the
# cluster write). Everything it decides from values lives here, so a test passes a parsed
# `ConfigPatchPlan` and asserts the refusal, the candidate patch or the result.

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from base.api_contracts.config import ConfigFieldWriteResult, ConfigWriteResult
from base.config.admin.editing import ConfigPatchPlan, split_reducer_patch
from base.config.admin.metadata import ConfigFieldMeta
from gateway.routers.refusal import HttpFailure
from ops.rpc_schemas import ConfigWriteOpResult

__all__ = [
    "CandidatePatch",
    "candidate_patch",
    "candidate_rejection",
    "plan_rejection",
    "restart_required",
    "stale_env_conflict",
    "write_result",
]

STALE_ENV_MESSAGE = ".env changed before owned runtime-config write"


# meiva:note
# The cluster `.env` patch a candidate validation judges.
@dataclass(frozen=True)
class CandidatePatch:
    writes: dict[str, object]
    removals: set[str]


# meiva:note
# The 400 a parsed PUT earns before anything is written, or None.
def plan_rejection(plan: ConfigPatchPlan) -> HttpFailure | None:
    if plan.violations:
        return HttpFailure(400, f"unknown or read-only fields: {sorted(plan.violations)}")
    if plan.scalar_error:
        return HttpFailure(400, plan.scalar_error)
    if plan.remote_cluster_keys:
        return HttpFailure(
            400, "cluster config is machine-independent; edit it on the Cluster view"
        )
    return None


# meiva:note
# The cluster patch to validate as a candidate `.env`.
#
# A local PUT writes its host fields into the same `.env`, so a host field that shares a
# domain with a changed cluster field is part of the candidate: a cross-field rule such as
# two timeouts is judged on the file the write will leave.
def candidate_patch(
    plan: ConfigPatchPlan,
    metas: Mapping[str, ConfigFieldMeta],
    domain_of: Mapping[str, str],
    *,
    local: bool,
) -> CandidatePatch:
    writes = dict(plan.cluster_writes)
    removals = set(plan.cluster_removals)
    cluster_domains = {domain_of[name] for name in set(writes) | removals}
    if local and cluster_domains:
        host_writes, host_removals = split_reducer_patch(plan.host_body, dict(metas))
        for name, value in host_writes.items():
            if domain_of[name] in cluster_domains:
                writes[name] = value
        removals.update(name for name in host_removals if domain_of[name] in cluster_domains)
    return CandidatePatch(writes, removals)


# meiva:note
# The 400 for a candidate `.env` that breaks an invariant, or None.
def candidate_rejection(errors: Sequence[str]) -> HttpFailure | None:
    if errors:
        return HttpFailure(400, "candidate config rejected: " + "; ".join(errors))
    return None


# meiva:note
# The retryable 409 for a write that found `.env` changed since validation, or None.
def stale_env_conflict(error: RuntimeError) -> HttpFailure | None:
    if str(error) == STALE_ENV_MESSAGE:
        return HttpFailure(409, "config changed concurrently; retry the request")
    return None


# meiva:note
# The restart targets of a write: the changed cluster fields' plus the host's, sorted.
def restart_required(
    metas: Mapping[str, ConfigFieldMeta], changed: Collection[str], host_restart: Collection[str]
) -> list[str]:
    cluster = {metas[name].restart_required for name in changed if metas[name].restart_required}
    return sorted(cluster | set(host_restart))


# meiva:note
# The frontend-facing result of a host write (the machine is dropped).
def write_result(host_result: ConfigWriteOpResult, restart: list[str]) -> ConfigWriteResult:
    return ConfigWriteResult(
        applied=host_result.applied,
        results={
            field: ConfigFieldWriteResult(ok=result.ok, reason=result.reason)
            for field, result in host_result.results.items()
        },
        restart_required=restart,
    )
