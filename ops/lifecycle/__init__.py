"""Home-machine launch and supported lifecycle operations, with explicit input wake."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import assert_never

from psycopg_pool import ConnectionPool

from base.agents.epochs.extent import extent_reader_at
from base.agents.inbox.insert import insert_inbound_message
from base.agents.inbox.wake import publish_inbound_wake
from base.agents.lifecycle import release_browser_pages
from base.agents.messages.inbound import WakeTriggerKind, validate_wake_trigger_kind
from base.agents.messages.lifecycle_text import fast_acknowledgement_note, harness_switch_note
from base.agents.model.overlay_columns import split_overlay_columns
from base.api_contracts.agent_contract import (
    AgentStatus,
    InterruptResult,
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
from base.db import Database
from base.db.transaction import write_transaction
from base.events.live.announce import publish_agent_updated_sync
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.lm.registry import normalize_overlay_llm_model
from base.sessions.pty.client import ServiceDownError
from ops.agents import (
    get_agent_machine,
    get_agent_status,
)
from ops.agents.resurrection_retry import report_auto_resurrect_failure
from ops.agents.wake import ResurrectTriggerStaleError, resurrect_agent
from ops.cluster import rpc as cluster_rpc
from ops.cluster_status import kill_agent_shells
from ops.lifecycle import resurrect_plan, termination
from ops.lifecycle.deferred_restart import defer_restart_blocking

# Re-exported from `events`; callers keep module-qualified sites.
from ops.lifecycle.events import (
    publish_inbound_arrived as publish_inbound_arrived,
)
from ops.lifecycle.events import (
    publish_question_asked as publish_question_asked,
)
from ops.lifecycle.events import (
    publish_question_resolved as publish_question_resolved,
)
from ops.lifecycle.hosted_turn import (
    LocalAgentHost,
    cancel_hosted_turn,
    cancel_hosted_turn_best_effort,
    prepare_harness_switch,
)
from ops.lifecycle.launch import (
    launch_agent_op as launch_agent_op,
)
from ops.lifecycle.restart_row import (
    read_restart_status_and_harness_blocking,
    update_harness_transition_meta_blocking,
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
    InterruptAgentResponse,
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
#     Every terminal transition closes the agent's shells on its home machine.
#     A force terminate closes them right after its row commits, and so does a
#     terminate that finds the agent already terminated. A graceful terminate
#     closes them after its last step commits (`agent.ownership.hosted.apply_hosted_lifecycle`).
#     The response's `shell_sessions` reports which of the two happened.
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
    attached = await asyncio.to_thread(termination.harness_attachment, agent_id, home=host.home)
    if body.force:
        _old_status, _command_id, cutoff = await asyncio.to_thread(
            terminate_force_blocking, db, bus, agent_id, body, db_pool
        )
        await cancel_hosted_turn_best_effort(agent_id, host=host)
        await release_browser_pages(agent_id, home=host.home)
        _log.info(
            "[gateway] agent %s force requested by %s",
            agent_id,
            body.source,
        )
        return TerminateAgentResponse(
            status=TerminateResult.ENQUEUED,
            shell_sessions=await _kill_shell_sessions_now(
                agent_id,
                cutoff=cutoff,
                db_pool=db_pool,
                home=host.home,
            ),
            attached=attached,
        )

    iid, cutoff = await asyncio.to_thread(
        _terminate_graceful_blocking, db, bus, agent_id, body, db_pool
    )
    if iid is None:
        # The kill-requesting enqueue found the row terminated under its lock.
        return TerminateAgentResponse(
            status=TerminateResult.ALREADY_TERMINATED,
            shell_sessions=await _kill_shell_sessions_now(
                agent_id, cutoff=cutoff, db_pool=db_pool, home=host.home
            ),
            attached=attached,
        )
    await publish_inbound_arrived(bus, agent_id, iid, "terminate", body.source, "")
    return TerminateAgentResponse(
        status=TerminateResult.ENQUEUED,
        shell_sessions=ShellSessionsKill(when=ShellSessionKillTiming.AT_EXIT),
        attached=attached,
    )


# meiva:note
# Attempt this host's older shells when a cutoff was captured, drop their TTL rows and report them.
#
# The PTY backend durably records the cutoff before dialing its service. When that service is
# absent, leave every TTL row in place for the cluster reaper and let the already-committed
# lifecycle operation finish. Other failures still propagate because they do not prove that the
# service is simply down. TTL rows are dropped only after a definitive fence acknowledgement.
async def _kill_shell_sessions_now(
    agent_id: int, *, cutoff: int | None, db_pool: ConnectionPool, home: Path
) -> ShellSessionsKill | None:
    if cutoff is None:
        return None
    try:
        cleanup = await asyncio.to_thread(
            kill_agent_shells, agent_id, before_session_index=cutoff, home=home
        )
    except ServiceDownError:
        _log.warning(
            "[gateway] agent %s shell cleanup deferred at cutoff %s: "
            "pty-sessions is unavailable; retaining TTL rows for the reaper",
            agent_id,
            cutoff,
        )
        return None
    if cleanup.fenced:
        await asyncio.to_thread(
            termination.drop_shell_ttls_before_cutoff, db_pool, agent_id, cutoff
        )
    killed = cleanup.killed
    _log.info(
        "[gateway] agent %s terminate killed %d shell session(s): %s",
        agent_id,
        len(killed),
        killed,
    )
    return ShellSessionsKill(when=ShellSessionKillTiming.NOW, killed=killed)


# meiva:note
# The interruption of a native agent's turn, after the gateway committed the note and the message.
# An external harness is interrupted through its interrupt row and the adapter instead: the host
# task owns the session, so cancelling that task would also close its background work.
async def interrupt_hosted_turn_op(
    agent_id: int, *, host: LocalAgentHost
) -> InterruptAgentResponse:
    cancelled = await cancel_hosted_turn(agent_id, host=host, rearm=True)
    return InterruptAgentResponse(
        status=InterruptResult.INTERRUPTED if cancelled else InterruptResult.NOT_RUNNING
    )


def terminate_force_blocking(
    db: Database,
    bus: EventBus,
    agent_id: int,
    body: TerminateAgentRequest,
    db_pool: ConnectionPool,
) -> tuple[AgentStatus, int, int | None]:
    """Commit the force fence before publishing its wake and lifecycle hint."""
    old_status, inbound_id, cutoff, ancestor_notice = termination.force_terminate_with_notice(
        agent_id,
        db_pool,
        source=body.source,
        message=body.message,
        kill_all_shell_sessions=body.kill_all_shell_sessions,
    )
    publish_force_terminate_inbound(db, bus, agent_id, inbound_id, body.source)
    if ancestor_notice is not None:
        publish_inbound_wake(db, bus, ancestor_notice.ancestor_id, str(ancestor_notice.inbound_id))
    publish_agent_updated_sync(bus, agent_id)
    return old_status, inbound_id, cutoff


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
    home: Path,
) -> int | None:
    from base.packages.plugins.config_registration import validate_config_overlay
    from base.packages.plugins.plugin_errors import InvalidConfigOverlay

    clean_overlay = split_overlay_columns(body.config_overlay).fields
    if clean_overlay:
        try:
            validate_config_overlay(
                clean_overlay,
                models=catalog.models,
                decision_models=catalog.decision_models,
                prices=catalog.prices,
                home=home,
            )
        except InvalidConfigOverlay as exc:
            raise InvalidModelConfig(str(exc)) from exc

    with write_transaction(db_pool) as conn:
        row = conn.execute(
            "SELECT status FROM agents_meta WHERE id=%s FOR UPDATE", (agent_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"agent {agent_id} does not exist")
        if row[0] == "terminated":
            return None
        payload: dict[str, object] | None = None
        if clean_overlay:
            from base.agents.model.model_config import validate_restart_model_config

            # Settle a withdrawn llm_model before the overlay is stored — the
            # provider-outage model-switch channel must not persist a stale id (#4306).
            model_receipt = normalize_overlay_llm_model(clean_overlay, models=catalog.models)
            with conn.cursor() as cur:
                validate_restart_model_config(
                    cur,
                    agent_id,
                    clean_overlay,
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
                (json.dumps(clean_overlay), agent_id),
            )
            payload = {"config_overlay": clean_overlay}
        # insert_inbound_message commits this connection, so its INSERT commits
        # the preceding overlay UPDATE in the same transaction.
        return insert_inbound_message(
            conn,
            agent_id,
            body.message or "",
            source=body.source,
            kind="restart",
            payload=payload,
            database=db,
            bus=bus,
        )


async def _execute_harness_transition(
    db: Database,
    bus: EventBus,
    agent_id: int,
    body: RestartAgentRequest,
    db_pool: ConnectionPool,
    *,
    status: str,
    old_harness: str,
    target_harness: str,
    is_switch: bool,
    host: LocalAgentHost,
    catalog: ModelCatalog,
    home: Path,
    environ: Mapping[str, str],
    require_quiescent: bool = False,
) -> RestartAgentResponse:
    if status not in ("idling", "terminated"):
        raise ValueError(f"Cannot restart agent in status {status!r}; must be idling or terminated")

    if require_quiescent:
        # meiva:note
        # Linearize the strict attempt with claim under the agent row lock before any host stop.
        _old_status, _cmd_id, _cutoff = await asyncio.to_thread(
            force_terminate_transaction,
            agent_id,
            db_pool,
            source=body.source,
            preserve_shells=True,
            require_quiescent=True,
        )
    await prepare_harness_switch(agent_id, host=host)
    if status != "terminated" and not require_quiescent:
        _old_status, _cmd_id, _cutoff = await asyncio.to_thread(
            force_terminate_transaction,
            agent_id,
            db_pool,
            source=body.source,
            preserve_shells=True,
        )

    await asyncio.to_thread(
        update_harness_transition_meta_blocking,
        db_pool,
        agent_id,
        target_harness,
        body,
        catalog,
        extent_reader_at(home, environ),
    )

    restart_note = (
        harness_switch_note(
            body.source, body.message, from_harness=old_harness, to_harness=target_harness
        )
        if is_switch
        else body.message
    )
    if update_note := fast_acknowledgement_note(body.config_overlay):
        restart_note = f"{restart_note}\n\n{update_note}" if restart_note else update_note
    await asyncio.to_thread(
        resurrect_agent, db, bus, agent_id, resurrected_by=body.source, restart_note=restart_note
    )
    return RestartAgentResponse(status=RestartResult.ENQUEUED)


# meiva:note
# Local-target restart or harness switch. A restart with `at_turn_end` (the agent's own) is
# only recorded here, as a pending command the host applies when the turn ends
# (`ops/lifecycle/deferred_restart.py`); every other restart acts now.
async def restart_agent_op(
    db: Database,
    bus: EventBus,
    agent_id: int,
    body: RestartAgentRequest,
    db_pool: ConnectionPool,
    *,
    host: LocalAgentHost,
    catalog: ModelCatalog,
    default_model: str,
    home: Path,
    environ: Mapping[str, str],
    require_quiescent: bool = False,
) -> RestartAgentResponse:
    if require_quiescent and body.at_turn_end:
        raise ValueError("scheduled attempts cannot use at_turn_end")
    if body.at_turn_end:
        iid = await asyncio.to_thread(
            defer_restart_blocking, db, bus, agent_id, body, db_pool, catalog=catalog
        )
        await publish_inbound_arrived(bus, agent_id, iid, "restart", "self", body.message)
        return RestartAgentResponse(status=RestartResult.ENQUEUED)

    status, old_harness = await asyncio.to_thread(
        read_restart_status_and_harness_blocking, db_pool, agent_id
    )
    requested_harness = split_overlay_columns(body.config_overlay).harness
    target_harness = old_harness if requested_harness is None else requested_harness.value

    is_switch = target_harness != old_harness
    if require_quiescent:
        return await _execute_harness_transition(
            db,
            bus,
            agent_id,
            body,
            db_pool,
            status=status,
            old_harness=old_harness,
            target_harness=target_harness,
            is_switch=is_switch,
            host=host,
            catalog=catalog,
            home=home,
            environ=environ,
            require_quiescent=True,
        )
    if not is_switch and status == "terminated":
        return RestartAgentResponse(status=RestartResult.ALREADY_TERMINATED)

    if not is_switch and old_harness == "meiva":
        iid = await asyncio.to_thread(
            restart_blocking,
            db,
            bus,
            agent_id,
            body,
            db_pool,
            catalog=catalog,
            default_model=default_model,
            home=home,
        )
        if iid is None:
            return RestartAgentResponse(status=RestartResult.ALREADY_TERMINATED)
        await publish_inbound_arrived(
            bus, agent_id, iid, "restart", body.source, body.message or ""
        )
        return RestartAgentResponse(status=RestartResult.ENQUEUED)

    return await _execute_harness_transition(
        db,
        bus,
        agent_id,
        body,
        db_pool,
        status=status,
        old_harness=old_harness,
        target_harness=target_harness,
        is_switch=is_switch,
        host=host,
        catalog=catalog,
        home=home,
        environ=environ,
    )


_LIFECYCLE_PATH = re.compile(
    r"^/api/agents/(?P<id>\d+)/"
    r"(?P<action>terminate|resurrect-explicit|"
    r"resurrect-if-pending-work|restart|restart-scheduled|interrupt)$"
)


# meiva:note
# This machine's agent host, from the settings and the home the op entry hands in.
#
# The op entry reads the port from the endpoint table it built and hands the value down; this
# module names neither a setting nor a home of its own.
def local_agent_host(home: Path, *, health_port: int) -> LocalAgentHost:
    return LocalAgentHost(home=home, health_port=health_port)


# meiva:note
# Dispatch terminate, restart, interrupt and explicit or exact-pending-input resurrection.
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
    environ: Mapping[str, str],
    agent_host_port: int,
    trigger_inbound_id: int | None = None,
    trigger_inbound_kind: WakeTriggerKind | None = None,
) -> (
    TerminateAgentResponse | ResurrectAgentResponse | RestartAgentResponse | InterruptAgentResponse
):
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
    if action == "interrupt":
        return await interrupt_hosted_turn_op(
            agent_id, host=local_agent_host(home, health_port=agent_host_port)
        )
    if action == "terminate":
        return await terminate_agent_op(
            db,
            bus,
            agent_id,
            TerminateAgentRequest.model_validate(body),
            db_pool,
            host=local_agent_host(home, health_port=agent_host_port),
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
    if action in ("restart", "restart-scheduled"):
        return await restart_agent_op(
            db,
            bus,
            agent_id,
            RestartAgentRequest.model_validate(body),
            db_pool,
            host=local_agent_host(home, health_port=agent_host_port),
            catalog=catalog,
            default_model=settings.lm.llm_model,
            home=home,
            environ=environ,
            require_quiescent=action == "restart-scheduled",
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
