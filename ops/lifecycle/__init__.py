"""Home-machine launch and supported lifecycle operations, with explicit input wake."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import assert_never

from psycopg_pool import ConnectionPool

from base.agents.inbox.insert import insert_inbound_message
from base.agents.lifecycle import release_browser_pages
from base.agents.messages.inbound import WakeTriggerKind, validate_wake_trigger_kind
from base.api_contracts.agent_contract import (
    AgentStatus,
    CancelResult,
    InvalidModelConfig,
    MachinePaused,
    RestartResult,
    ResurrectAlreadyAlive,
    ResurrectError,
    ResurrectResult,
    ShellSessionKillTiming,
    TerminateResult,
)
from base.config import settings
from base.daemon.endpoints import ServiceEndpoints
from base.db import Database
from base.events.live.announce import publish_agent_updated_sync
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.lm.registry import normalize_overlay_llm_model
from ops.agents import (
    get_agent_machine,
    get_agent_status,
    resurrect_agent,
)
from ops.agents.resurrection_retry import report_auto_resurrect_failure
from ops.agents.wake import ResurrectTriggerStaleError
from ops.cluster import rpc as cluster_rpc
from ops.cluster_status import kill_agent_shells
from ops.lifecycle import resurrect_plan, termination

# Re-exported from `events`; callers keep module-qualified sites.
from ops.lifecycle.events import (
    NoticeResolutionFacts as NoticeResolutionFacts,
)
from ops.lifecycle.events import (
    notice_resolution_facts as notice_resolution_facts,
)
from ops.lifecycle.events import (
    publish_inbound_arrived as publish_inbound_arrived,
)
from ops.lifecycle.events import (
    publish_notice_posted as publish_notice_posted,
)
from ops.lifecycle.events import (
    publish_notice_resolved as publish_notice_resolved,
)
from ops.lifecycle.events import (
    publish_page_closed as publish_page_closed,
)
from ops.lifecycle.hosted_turn import (
    LocalAgentHost,
    cancel_hosted_turn,
    cancel_hosted_turn_best_effort,
)

# Re-exported (explicit-alias form) from `launch` so the gateway routers and
# tests keep their module-qualified call sites.
from ops.lifecycle.launch import (
    launch_agent_op as launch_agent_op,
)

# Re-exported (explicit-alias form) from `resurrect_gates` so the
# module-qualified callers — tests — keep their call sites.
from ops.lifecycle.resurrect_gates import (
    clear_wake_suppression as clear_wake_suppression,
)
from ops.lifecycle.resurrect_gates import (
    recovery_halted as recovery_halted,
)
from ops.lifecycle.resurrect_gates import (
    system_notice_source_of_trigger as system_notice_source_of_trigger,
)
from ops.lifecycle.termination import (
    force_mark_terminated as force_mark_terminated,
)
from ops.lifecycle.termination import (
    force_terminate_transaction as force_terminate_transaction,
)
from ops.lifecycle.termination import (
    publish_force_terminate_inbound as publish_force_terminate_inbound,
)
from ops.rpc_schemas import (
    CancelAgentResponse,
    RestartAgentRequest,
    RestartAgentResponse,
    ResurrectAgentRequest,
    ResurrectAgentResponse,
    TerminateAgentRequest,
    TerminateAgentResponse,
)
from ops.rpc_schemas.terminate import ShellSessionsKill

_log = logging.getLogger(__name__)


# meiva:note
# Local-target graceful or force terminate. Caller handles cross-machine.
#
#     `body.kill_all_shell_sessions` also kills the agent's shell sessions on
#     this, its home machine (docs/decisions/agents/lifecycle/2026-09-27-terminate-has-no-closed-state.md).
#     A force terminate kills them right after its row commits, and so does a
#     terminate that finds the agent already terminated. A graceful terminate of
#     a live agent only records the request on its terminate command: the home
#     runtime attempts older sessions after the termination commits, after the
#     agent's last step (`agent.ownership.hosted.apply_hosted_lifecycle`). The
#     response's `shell_sessions` reports which of the two happened.
#
#     A force terminate cancels the running turn through `host`, releases the agent's
#     browser pages and kills shells through the real local session backend. Each of
#     those effects follows the committed row, so a failure after the commit leaves the
#     termination durable.
async def terminate_agent_op(
    db: Database,
    bus: EventBus,
    agent_id: int,
    body: TerminateAgentRequest,
    db_pool: ConnectionPool,
    *,
    host: LocalAgentHost,
) -> TerminateAgentResponse:
    if body.force:
        _old_status, killed_page_names, _command_id, cutoff = await asyncio.to_thread(
            terminate_force_blocking, db, bus, agent_id, body, db_pool
        )
        await cancel_hosted_turn_best_effort(agent_id, host=host)
        await release_browser_pages(agent_id, home=host.home)
        for page_name in killed_page_names:
            await publish_page_closed(bus, agent_id, page_name)
        _log.info(
            "[gateway] agent %s force requested by %s",
            agent_id,
            body.source,
        )
        return TerminateAgentResponse(
            status=TerminateResult.ENQUEUED,
            shell_sessions=await _kill_shell_sessions_now(
                agent_id,
                cutoff=cutoff if body.kill_all_shell_sessions else None,
                home=host.home,
            ),
        )

    if (
        not body.kill_all_shell_sessions
        and (await asyncio.to_thread(get_agent_status, db, agent_id)) is AgentStatus.TERMINATED
    ):
        return TerminateAgentResponse(
            status=TerminateResult.ALREADY_TERMINATED,
        )

    iid, cutoff = await asyncio.to_thread(
        _terminate_graceful_blocking, db, bus, agent_id, body, db_pool
    )
    if iid is None:
        # The kill-requesting enqueue found the row terminated under its lock.
        return TerminateAgentResponse(
            status=TerminateResult.ALREADY_TERMINATED,
            shell_sessions=await _kill_shell_sessions_now(agent_id, cutoff=cutoff, home=host.home),
        )
    await publish_inbound_arrived(bus, agent_id, iid, "terminate", body.source, "")
    return TerminateAgentResponse(
        status=TerminateResult.ENQUEUED,
        shell_sessions=ShellSessionsKill(when=ShellSessionKillTiming.AT_EXIT)
        if body.kill_all_shell_sessions
        else None,
    )


# meiva:note
# Attempt this host's older shells when a cutoff was captured, and report them.
#
# A failed kill propagates: the termination itself is already durable, so the caller sees
# the failure and a repeat request retries the kill.
async def _kill_shell_sessions_now(
    agent_id: int, *, cutoff: int | None, home: Path
) -> ShellSessionsKill | None:
    if cutoff is None:
        return None
    killed = await asyncio.to_thread(
        kill_agent_shells, agent_id, before_session_index=cutoff, home=home
    )
    _log.info(
        "[gateway] agent %s terminate killed %d shell session(s): %s",
        agent_id,
        len(killed),
        killed,
    )
    return ShellSessionsKill(when=ShellSessionKillTiming.NOW, killed=killed)


# meiva:note
# Local-target cancel of the running turn; the agent stays alive and idles.
#
#     The home agent host is asked to cancel the turn (`cancel_hosted_turn`). Unknown
#     agents raise; a host that cannot be reached raises too, because the caller asked
#     for a stop and must not be told nothing happened.
async def cancel_agent_op(
    db: Database, agent_id: int, *, host: LocalAgentHost
) -> CancelAgentResponse:
    await asyncio.to_thread(get_agent_status, db, agent_id)
    cancelled = await cancel_hosted_turn(agent_id, host=host)
    return CancelAgentResponse(
        status=CancelResult.CANCELLED if cancelled else CancelResult.NOT_RUNNING
    )


def terminate_force_blocking(
    db: Database,
    bus: EventBus,
    agent_id: int,
    body: TerminateAgentRequest,
    db_pool: ConnectionPool,
) -> tuple[AgentStatus, list[str], int, int]:
    """Commit the force fence before publishing its wake and lifecycle hint."""
    old_status, killed_page_names, inbound_id, cutoff = force_terminate_transaction(
        agent_id,
        db_pool,
        source=body.source,
        message=body.message,
        kill_all_shell_sessions=body.kill_all_shell_sessions,
    )
    publish_force_terminate_inbound(db, bus, agent_id, inbound_id, body.source)
    publish_agent_updated_sync(bus, agent_id)
    return old_status, killed_page_names, inbound_id, cutoff


def _terminate_graceful_blocking(
    db: Database,
    bus: EventBus,
    agent_id: int,
    body: TerminateAgentRequest,
    db_pool: ConnectionPool,
) -> tuple[int | None, int | None]:
    """Return a durable command ID, or an already-terminated shell cutoff."""
    return termination.enqueue_termination_inbounds(
        db,
        bus,
        agent_id,
        db_pool,
        source=body.source,
        message=body.message,
        kill_all_shell_sessions=body.kill_all_shell_sessions,
    )


async def resurrect_agent_op(
    db: Database,
    bus: EventBus,
    agent_id: int,
    body: ResurrectAgentRequest,
    *,
    trigger_inbound_id: int | None = None,
    trigger_inbound_kind: WakeTriggerKind | None = None,
) -> ResurrectAgentResponse:
    """Commit hosted resurrection intent, then publish a wake to the home host.

    `trigger_inbound_id` carries the internal auto-resurrect CAS to the home
    runner; manual resurrects omit it and may reopen a closed hosted agent.
    """
    s = await asyncio.to_thread(get_agent_status, db, agent_id)
    if s is not AgentStatus.TERMINATED:
        return ResurrectAgentResponse(status=ResurrectResult.ALREADY_ALIVE)
    try:
        # Keep the synchronous row-lock transaction and wake publication off
        # the gateway event loop. The agent host admits the successor later.
        await asyncio.to_thread(
            resurrect_agent,
            db,
            bus,
            agent_id,
            resurrected_by=body.resurrected_by,
            prompt=body.prompt,
            trigger_inbound_id=trigger_inbound_id,
            trigger_inbound_kind=trigger_inbound_kind,
        )
    except (ResurrectAlreadyAlive, ResurrectTriggerStaleError):
        return ResurrectAgentResponse(status=ResurrectResult.ALREADY_ALIVE)
    return ResurrectAgentResponse(status=ResurrectResult.SPAWNED)


def _read_resurrect_facts(
    db: Database,
    agent_id: int,
    trigger_inbound_id: int,
    trigger_inbound_kind: WakeTriggerKind,
    local_machine: str,
) -> resurrect_plan.ResurrectFacts:
    return resurrect_plan.ResurrectFacts(
        agent_id=agent_id,
        trigger_inbound_id=trigger_inbound_id,
        trigger_kind=trigger_inbound_kind,
        recovery_halted=recovery_halted(db, agent_id),
        notice_source=system_notice_source_of_trigger(db, agent_id, trigger_inbound_id),
        home=get_agent_machine(db, agent_id),
        local_machine=local_machine,
    )


# meiva:note
# Try one home-machine wake for an exact newly delivered input.
#
#     Fresh chat can wake a terminated agent after a fix. Background heartbeat and other
#     automatic triggers stop after a permanent provider rejection. System chat notices
#     cannot resurrect, including old hosted-turn recovery markers. The home CAS verifies
#     the pending input against the shared kill-command fence
#     (`TRIGGER_AFTER_KILL_COMMAND`). This call itself does not retry a failed attempt:
#     unknown errors reach the sender, known placement and refusal conditions are logged
#     with the input queued, and the delivery watchdog's stranded-wake fallback retries
#     the same wake on its own cooldown until the dead-letter bound. A new message or
#     explicit operator resurrection can also try again.
#
#     The trigger kind is canonicalized before any database read. A live agent then ends the
#     call after its one status read. For a terminated agent the shell
#     reads the recovery breaker, the trigger's notice source and the home machine, and
#     `resurrect_plan.plan_resurrection` decides between skipping and forwarding.
async def resurrect_if_terminated(
    db: Database,
    bus: EventBus,
    agent_id: int,
    *,
    trigger_inbound_id: int,
    trigger_inbound_kind: WakeTriggerKind,
    local_machine: str,
    rpc: cluster_rpc.RpcClient,
) -> AgentStatus:
    trigger_inbound_kind = validate_wake_trigger_kind(trigger_inbound_kind)
    status = await asyncio.to_thread(get_agent_status, db, agent_id)
    if status is not AgentStatus.TERMINATED:
        return status
    facts = await asyncio.to_thread(
        _read_resurrect_facts, db, agent_id, trigger_inbound_id, trigger_inbound_kind, local_machine
    )
    plan = resurrect_plan.plan_resurrection(facts)
    match plan:
        case resurrect_plan.RecoveryHalted():
            _log.warning(
                "automatic wake stopped after a permanent provider error: agent %s", agent_id
            )
            return status
        case resurrect_plan.NotificationOnly(source=source):
            _log.debug(
                "resurrect_if_terminated: trigger %s for agent %s is a system notice "
                "(source=%s); notifications never resurrect a terminated owner — "
                "skipping auto-resurrect",
                trigger_inbound_id,
                agent_id,
                source,
            )
            return status
        case resurrect_plan.Forward():
            return await _forward_resurrection(db, bus, agent_id, plan, status=status, rpc=rpc)
        case _:
            assert_never(plan)


# meiva:note
# Run the forward plan: the home machine's ops server, else this process when this is home.
#
# An unreachable home is logged with the input queued, a refusal is reported the same way,
# and any other failure reaches the sender.
async def _forward_resurrection(
    db: Database,
    bus: EventBus,
    agent_id: int,
    plan: resurrect_plan.Forward,
    *,
    status: AgentStatus,
    rpc: cluster_rpc.RpcClient,
) -> AgentStatus:
    body = ResurrectAgentRequest(resurrected_by="system")
    try:
        try:
            forwarded = await rpc.dispatch_to_machine(
                db,
                target_machine=plan.home,
                kind="lifecycle",
                payload={
                    "path": plan.path,
                    "body": body.model_dump(),
                    "trigger_inbound_id": plan.trigger_inbound_id,
                    "trigger_inbound_kind": plan.trigger_inbound_kind,
                },
            )
            result = ResurrectAgentResponse.model_validate(forwarded).status
        except cluster_rpc.ClusterOpUnreachable:
            if not plan.run_here_if_unreachable:
                raise
            result = (
                await resurrect_agent_op(
                    db,
                    bus,
                    agent_id,
                    body,
                    trigger_inbound_id=plan.trigger_inbound_id,
                    trigger_inbound_kind=plan.trigger_inbound_kind,
                )
            ).status
        for step in resurrect_plan.plan_after_result(result):
            match step:
                case resurrect_plan.ClearWakeSuppression():
                    await asyncio.to_thread(clear_wake_suppression, db, agent_id)
                case resurrect_plan.RereadStatus():
                    status = await asyncio.to_thread(get_agent_status, db, agent_id)
                case _:
                    assert_never(step)
    except cluster_rpc.ClusterOpUnreachable as exc:
        _log.info(
            "resurrect_if_terminated: agent %s home machine unreachable, skipping "
            "auto-resurrect (%s); inbound queued — the next delivery or a manual "
            "resurrect picks it up once the machine is back",
            agent_id,
            exc,
        )
    except (ResurrectError, MachinePaused) as exc:
        report_auto_resurrect_failure(agent_id, exc)
    return status


# meiva:note
# Sync restart section, run through `to_thread`. Returns the inbound id, or None when the agent
# is already terminated. `default_model` is the model an agent runs on when neither its overlay
# nor its birth config names one.
def restart_blocking(
    db: Database,
    bus: EventBus,
    agent_id: int,
    body: RestartAgentRequest,
    db_pool: ConnectionPool,
    *,
    catalog: ModelCatalog,
    default_model: str,
) -> int | None:
    from base.db.transaction import write_transaction
    from base.packages.plugins.config_registration import validate_config_overlay
    from base.packages.plugins.plugin_errors import InvalidConfigOverlay

    if body.config_overlay:
        try:
            validate_config_overlay(
                body.config_overlay,
                models=catalog.models,
                decision_models=catalog.decision_models,
            )
        except InvalidConfigOverlay as exc:
            raise InvalidModelConfig(str(exc)) from exc

    with write_transaction(db_pool) as conn:
        row = conn.execute(
            "SELECT status, harness FROM agents_meta WHERE id=%s FOR UPDATE", (agent_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"agent {agent_id} does not exist")
        if row[1] != "meiva":
            raise ValueError(
                f"Restart is not supported for external harness '{row[1]}'; "
                f"use terminate and resurrect instead."
            )
        if row[0] == "terminated":
            return None
        payload: dict[str, object] | None = None
        if body.config_overlay:
            from base.agents.model.model_config import validate_restart_model_config

            # Settle a withdrawn llm_model before the overlay is stored — the
            # provider-outage model-switch channel must not persist a stale id (#4306).
            overlay = dict(body.config_overlay)
            model_receipt = normalize_overlay_llm_model(overlay, models=catalog.models)
            with conn.cursor() as cur:
                validate_restart_model_config(
                    cur,
                    agent_id,
                    overlay,
                    catalog=catalog,
                    default_model=default_model,
                )
            if model_receipt is not None:
                _log.warning(
                    "restart overlay llm_model %r is withdrawn; storing the "
                    "registered fallback %r (task #4306)",
                    model_receipt[0],
                    model_receipt[1],
                )
            # This overlay write must not depend on borrowed-backend session
            # state: PgBouncer can hand us a backend whose session a previous
            # client left with default_transaction_read_only=on, and this
            # emergency channel (provider-outage model switch) is exactly when
            # the pool is most likely poisoned. Declare the transaction
            # writable before the DML (same posture as #1428/#1436).
            conn.execute(
                "UPDATE agents_meta "
                "SET config_overlay = COALESCE(config_overlay, '{}'::jsonb) || %s::jsonb "
                "WHERE id = %s",
                (json.dumps(overlay), agent_id),
            )
            payload = {"config_overlay": overlay}
        # insert_inbound_message commits this connection, so its INSERT commits
        # the preceding overlay UPDATE in the same transaction.
        return insert_inbound_message(
            conn,
            agent_id,
            "",
            source=body.source,
            kind="restart",
            payload=payload,
            database=db,
            bus=bus,
        )


async def restart_agent_op(
    db: Database,
    bus: EventBus,
    agent_id: int,
    body: RestartAgentRequest,
    db_pool: ConnectionPool,
    *,
    catalog: ModelCatalog,
    default_model: str,
) -> RestartAgentResponse:
    """Local-target restart — INSERT one kind='restart' inbound."""
    iid = await asyncio.to_thread(
        restart_blocking,
        db,
        bus,
        agent_id,
        body,
        db_pool,
        catalog=catalog,
        default_model=default_model,
    )
    if iid is None:
        return RestartAgentResponse(status=RestartResult.ALREADY_TERMINATED)
    await publish_inbound_arrived(bus, agent_id, iid, "restart", body.source, "")
    return RestartAgentResponse(status=RestartResult.ENQUEUED)


_LIFECYCLE_PATH = re.compile(
    r"^/api/agents/(?P<id>\d+)/"
    r"(?P<action>terminate|resurrect-explicit|"
    r"resurrect-if-pending-work|restart|cancel)$"
)


# meiva:note
# This machine's agent host, from the settings and the home the op entry hands in.
#
# `ops/ambient_roots.toml` names this module as the one place of the package that builds the
# endpoint table, so the op entry reads the port once and hands the value down.
def _local_agent_host(home: Path) -> LocalAgentHost:
    return LocalAgentHost(
        home=home,
        health_port=ServiceEndpoints.from_settings().of("agent_host").health_port,
    )


# meiva:note
# Dispatch terminate, restart, cancel and explicit or exact-pending-input resurrection.
#
#     Retired billing recovery and crash-harvest paths fail validation without effects.
#     Unknown paths raise ValueError, which the ops server exposes as a failed result.
async def lifecycle_op(
    db: Database,
    bus: EventBus,
    path: str,
    body: dict[str, object],
    db_pool: ConnectionPool,
    *,
    catalog: ModelCatalog,
    home: Path,
    trigger_inbound_id: int | None = None,
    trigger_inbound_kind: WakeTriggerKind | None = None,
) -> TerminateAgentResponse | ResurrectAgentResponse | RestartAgentResponse | CancelAgentResponse:
    m = _LIFECYCLE_PATH.match(path)
    if m is None:
        raise ValueError(f"lifecycle path not recognized: {path!r}")
    agent_id = int(m.group("id"))
    action = m.group("action")
    if (trigger_inbound_id is None) != (trigger_inbound_kind is None):
        raise ValueError("trigger inbound id and kind must be provided together")
    if action != "resurrect-if-pending-work" and trigger_inbound_id is not None:
        raise ValueError(
            f"trigger inbound is only valid for resurrect-if-pending-work, not {action!r}"
        )
    if action == "cancel":
        return await cancel_agent_op(db, agent_id, host=_local_agent_host(home))
    if action == "terminate":
        return await terminate_agent_op(
            db,
            bus,
            agent_id,
            TerminateAgentRequest.model_validate(body),
            db_pool,
            host=_local_agent_host(home),
        )
    if action == "resurrect-explicit":
        return await resurrect_agent_op(
            db,
            bus,
            agent_id,
            ResurrectAgentRequest.model_validate(body),
        )
    if action == "resurrect-if-pending-work":
        request = _pending_work_resurrection_request(body, trigger_inbound_id)
        return await resurrect_agent_op(
            db,
            bus,
            agent_id,
            request,
            trigger_inbound_id=trigger_inbound_id,
            trigger_inbound_kind=trigger_inbound_kind,
        )
    if action == "restart":
        return await restart_agent_op(
            db,
            bus,
            agent_id,
            RestartAgentRequest.model_validate(body),
            db_pool,
            catalog=catalog,
            default_model=settings.lm.llm_model,
        )
    raise AssertionError(f"unreachable: action={action!r}")


def _pending_work_resurrection_request(
    body: dict[str, object], trigger_inbound_id: int | None
) -> ResurrectAgentRequest:
    """Keep the guarded resurrection boundary independent of action routing."""
    if trigger_inbound_id is None:
        raise ValueError("resurrect-if-pending-work requires trigger inbound")
    request = ResurrectAgentRequest.model_validate(body)
    if request.resurrected_by != "system":
        raise ValueError("resurrect-if-pending-work requires resurrected_by='system'")
    return request
