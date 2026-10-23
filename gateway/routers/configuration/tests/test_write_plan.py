# meiva:note
# `PUT /api/config` decisions: what is refused, what a candidate `.env` contains, what a stale
# write answers. Values in, the returned plan asserted.

from __future__ import annotations

from base.api_contracts.config import ConfigWriteResult
from base.config.admin.editing import ConfigPatchPlan
from base.config.service_read import ConfigAuthority
from base.host.env.config_lite_table import FIELD_DOMAINS
from base.packages.plugin_config_admin import panel_metadata
from gateway.routers.configuration import write_plan
from gateway.routers.refusal import HttpFailure
from ops.rpc_schemas import ConfigWriteOpResult, FieldWriteResult


def _plan(
    *,
    host_body: dict[str, object] | None = None,
    cluster_writes: dict[str, object] | None = None,
    cluster_removals: set[str] | None = None,
    **notes: object,
) -> ConfigPatchPlan:
    return ConfigPatchPlan(
        host_body=host_body or {},
        cluster_writes=cluster_writes or {},
        cluster_removals=cluster_removals or set(),
        # meiva:note
        # The keyword arguments are spread from a dict of changed fields, which pyright cannot match to the signature.
        **notes,  # pyright: ignore[reportArgumentType]
    )


def test_a_plan_with_violations_scalar_errors_or_remote_cluster_keys_is_refused() -> None:
    assert write_plan.plan_rejection(_plan()) is None
    unknown = write_plan.plan_rejection(_plan(violations=("b", "a")))
    assert unknown == HttpFailure(400, "unknown or read-only fields: ['a', 'b']")
    scalar = write_plan.plan_rejection(_plan(scalar_error="n: invalid int value 'x'"))
    assert scalar == HttpFailure(400, "n: invalid int value 'x'")
    remote = write_plan.plan_rejection(_plan(remote_cluster_keys=("llm_model",)))
    assert remote is not None and "machine-independent" in remote.detail


def test_a_host_only_put_has_an_empty_candidate() -> None:
    patch = write_plan.candidate_patch(
        _plan(host_body={"ops_concurrency": 4}), {}, FIELD_DOMAINS, local=True
    )
    assert (patch.writes, patch.removals) == ({}, set())


def test_a_local_candidate_carries_the_host_fields_of_a_changed_domain(
    config_authority: ConfigAuthority,
) -> None:
    metas = {meta.name: meta for meta in panel_metadata(authority=config_authority)}
    # meiva:note
    # `delivery_watchdog_interval_seconds` and `heartbeat_enabled` share the daemon domain;
    # `ops_concurrency` does not.
    assert FIELD_DOMAINS["delivery_watchdog_interval_seconds"] == FIELD_DOMAINS["heartbeat_enabled"]
    assert FIELD_DOMAINS["ops_concurrency"] != FIELD_DOMAINS["heartbeat_enabled"]
    plan = _plan(
        host_body={"heartbeat_enabled": False, "ops_concurrency": 4},
        cluster_writes={"delivery_watchdog_interval_seconds": 30},
    )
    local = write_plan.candidate_patch(plan, metas, FIELD_DOMAINS, local=True)
    assert local.writes == {"delivery_watchdog_interval_seconds": 30, "heartbeat_enabled": False}
    remote = write_plan.candidate_patch(plan, metas, FIELD_DOMAINS, local=False)
    assert remote.writes == {"delivery_watchdog_interval_seconds": 30}


def test_candidate_errors_are_a_400_listing_each() -> None:
    assert write_plan.candidate_rejection([]) is None
    assert write_plan.candidate_rejection(["a", "b"]) == HttpFailure(
        400, "candidate config rejected: a; b"
    )


def test_only_the_stale_env_message_is_a_conflict() -> None:
    stale = write_plan.stale_env_conflict(RuntimeError(write_plan.STALE_ENV_MESSAGE))
    assert stale == HttpFailure(409, "config changed concurrently; retry the request")
    assert write_plan.stale_env_conflict(RuntimeError("disk full")) is None


def test_the_write_result_drops_the_machine_and_keeps_each_verdict() -> None:
    host = ConfigWriteOpResult(
        machine="m",
        results={"ops_concurrency": FieldWriteResult(ok=False, reason="must be an integer")},
        applied=False,
        restart_required=[],
    )
    assert write_plan.write_result(host, ["ops"]) == ConfigWriteResult.model_validate(
        {
            "applied": False,
            "results": {"ops_concurrency": {"ok": False, "reason": "must be an integer"}},
            "restart_required": ["ops"],
        }
    )
