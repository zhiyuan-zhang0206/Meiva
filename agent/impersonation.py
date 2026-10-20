"""Cooperative takeover at the native runtime's durable invocation boundary.

The external holder never writes a graph checkpoint. Accepted requests stop
native nodes; only the invocation driver activates after exec closure and the
checkpointer flush. A database lease gates every subsequent invocation.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command
from psycopg_pool import AsyncConnectionPool

from agent import impersonation_handoff as _handoff
from agent import impersonation_process as _relay_process
from agent import state as _state
from agent.messages import inbound_message
from agent.nodes import BEFORE_LLM, END, NodeName
from base.agents.context import MeivaContext, agent_id_from_config
from base.agents.impersonation.notes import HandoffNotes
from base.agents.impersonation.relay import record_relay_failure
from base.agents.impersonation.relay import stamp_relay_failure as stamp_relay_failure
from base.agents.impersonation.status import OPEN, ImpersonationStatus
from base.agents.messages.envelope import EnvelopeReadInputs, inbound_head
from base.agents.observation.relay_supervision import RelayChild, RelaySupervision, relay_exited
from base.agents.observation.relay_supervision import heartbeat_fresh as _heartbeat_fresh
from base.agents.observation.relay_supervision import terminate_relay as terminate_relay
from base.db import Database
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation


async def native_status(
    db: Database, bus: EventBus, agent_id: int, *, incarnation: RuntimeIncarnation | None
) -> dict[str, Any] | None:
    """Read authoritative lease state only for an admitted native runtime."""
    if incarnation is None:
        return None
    from base.agents.impersonation import native_status as read_status

    return await asyncio.to_thread(read_status, db, bus, agent_id, incarnation)


async def lifecycle_ready(pool: AsyncConnectionPool, agent_id: int) -> bool:
    """Native restart/terminate can run while the external owner handles cancel."""
    async with pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM inbound_messages i WHERE i.agent_id=%s "
            "AND i.kind IN ('restart','terminate') "
            "AND i.status IN ('pending','claimed') LIMIT 1",
            (agent_id,),
        )
        return await cursor.fetchone() is not None


async def active_lease(pool: AsyncConnectionPool, agent_id: int) -> bool:
    """Pre-admission host gate; accepted rows must reach a durable boundary."""
    async with pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM agent_impersonations WHERE agent_id=%s "
            "AND status='active' AND expires_at>clock_timestamp() LIMIT 1",
            (agent_id,),
        )
        return await cursor.fetchone() is not None


async def claim_gate(
    state: _state.AgentState, agent_id: int, ctx: MeivaContext
) -> Command[NodeName] | None:
    """Present consent once, or end the invocation without claiming input."""
    incarnation = ctx.original_incarnation
    db, bus = ctx.require_db(), ctx.require_bus()
    session = await native_status(db, bus, agent_id, incarnation=incarnation)
    await supervise_relay(
        db,
        bus,
        session,
        agent_id,
        ctx.relays,
        incarnation=incarnation,
    )
    if session is None:
        return None
    if session["status"] == ImpersonationStatus.REQUESTED and session["automatic"]:
        from base.agents.impersonation import accept

        assert incarnation is not None  # noqa: S101 — native_status requires it
        brief = session["reason"] or "Continue the agent's work from its saved state."
        await asyncio.to_thread(
            accept,
            db,
            bus,
            session["id"],
            agent_id,
            incarnation,
            brief,
            local_machine=ctx.require_machine().name(),
        )
        return Command(
            update={
                **_handoff.start_update(
                    session,
                    introduced=state.impersonation_introduced,
                    notes=HandoffNotes(
                        ctx.require_clock,
                        lambda: ctx.require_agent().read("general", "message_timestamps"),
                    ),
                ),
                "turn_active": False,
                "turn_idle": True,
            },
            goto=END,
        )
    if session["status"] == ImpersonationStatus.REQUESTED:
        request_receipt = f"{session['id']}:{session['consent_version']}"
        if state.impersonation_request_id == request_receipt:
            return None
        request_id = session["id"]
        content = (
            f"External agent requests to impersonate you. Request: {request_id}.\n"
            f"Reason: {session['reason']}\n"
            "To accept, save your working state and call "
            f"meiva.impersonation.accept({request_id!r}, start_message='your handoff "
            "brief for the external session'); this ends your execution. The brief "
            "is delivered to the external session when the takeover starts and is "
            "required: cover the current work, the context it needs, and how to "
            "acknowledge incoming messages. "
            f"To decline, call meiva.impersonation.reject({request_id!r}, reason=...). "
            "Acceptance pauses your native loop until release or lease expiry."
        )
        head = inbound_head(
            session["source"],
            inputs=EnvelopeReadInputs(
                ctx.require_clock, lambda: ctx.require_agent().read("general", "message_timestamps")
            ),
        )
        message = inbound_message(
            message_id=f"impersonation-request:{request_receipt}",
            content=head + content,
            source=session["source"],
            inbound_id=None,
            body_start=len(head),
            created_at=datetime.now(UTC),
        )
        return Command(
            update={
                "messages": [message],
                "impersonation_request_id": request_receipt,
                "halted": False,
                "turn_active": True,
            },
            goto=BEFORE_LLM,
        )
    # Terminal rows with an unapplied delta also stop here: the invocation
    # driver applies the ordered log before allowing another model decision.
    return Command(update={"turn_active": False, "turn_idle": True}, goto=END)


def protect_native_hooks(
    runner: Callable[..., Awaitable[Command[NodeName]]],
) -> Callable[..., Awaitable[Command[NodeName]]]:
    """Fence automatic compaction and plugin hooks before the LLM node."""

    async def guarded(
        state: _state.AgentState, runtime: Runtime[MeivaContext], config: RunnableConfig
    ) -> Command[NodeName]:
        if runtime.context.ops_pool is not None:
            session = await native_status(
                runtime.context.require_db(),
                runtime.context.require_bus(),
                agent_id_from_config(config),
                incarnation=runtime.context.original_incarnation,
            )
            if session is not None and session["status"] != ImpersonationStatus.REQUESTED:
                return Command(update={"turn_idle": True}, goto=END)
        return await runner(state, runtime, config)

    return guarded


async def _activate_accepted(
    graph: CompiledStateGraph[
        _state.BaseAgentState, MeivaContext, _state.BaseAgentState, _state.BaseAgentState
    ],
    db: Database,
    bus: EventBus,
    session: dict[str, Any],
    incarnation: RuntimeIncarnation,
    relays: RelaySupervision,
    *,
    notes: HandoffNotes,
    read_local_machine: Callable[[], str],
) -> dict[str, Any] | None:
    """Activate an accepted lease; None when the relay gate failed and native control resumes."""
    from base.agents.impersonation import activate

    await _handoff.ensure_start_marker(graph, session, notes=notes)
    # The bound relay must be live before the takeover stands. On failure
    # the lease is rolled back to 'rejected' with a loud reason and the
    # native agent resumes — no silent half-takeover.
    if not await asyncio.to_thread(
        establish_relay,
        db,
        bus,
        session,
        incarnation,
        relays,
        read_local_machine=read_local_machine,
    ):
        return None
    return await asyncio.to_thread(activate, db, bus, session["id"], incarnation)


async def _apply_plugin_deltas(
    graph: CompiledStateGraph[
        _state.BaseAgentState, MeivaContext, _state.BaseAgentState, _state.BaseAgentState
    ],
    db: Database,
    session: dict[str, Any],
    agent_id: int,
    incarnation: RuntimeIncarnation,
) -> None:
    """Apply each unapplied plugin delta exactly once, recording its receipt."""
    from agent.extensions.plugin_state_codec import decode_plugin_delta
    from base.agents.impersonation import mark_plugin_applied

    config: RunnableConfig = {"configurable": {"thread_id": str(agent_id)}}
    snapshot = await graph.aget_state(config)
    receipt = snapshot.values.get("impersonation_applied", {})
    for version in range(session["applied_version"] + 1, session["delta_version"] + 1):
        expected = {"lease_id": session["id"], "version": version}
        recorded_version = (
            receipt.get("version", 0) if receipt.get("lease_id") == session["id"] else 0
        )
        if recorded_version < version:
            delta = decode_plugin_delta(
                session["plugin_delta"][version - 1], graph.builder.state_schema
            )
            await graph.aupdate_state(config, {**delta, "impersonation_applied": expected})
            await _handoff.flush_checkpoint(graph.checkpointer, agent_id)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            receipt = expected
        await asyncio.to_thread(mark_plugin_applied, db, session["id"], version, incarnation)


async def settle_checkpoint(
    graph: CompiledStateGraph[
        _state.BaseAgentState, MeivaContext, _state.BaseAgentState, _state.BaseAgentState
    ],
    db: Database,
    bus: EventBus,
    agent_id: int,
    relays: RelaySupervision,
    *,
    activate_accepted: bool = True,
    incarnation: RuntimeIncarnation | None,
    notes: HandoffNotes,
    read_local_machine: Callable[[], str],
) -> bool:
    """After invocation+flush, activate; apply terminal deltas exactly once."""
    session = await native_status(db, bus, agent_id, incarnation=incarnation)
    if session is None or session["status"] == ImpersonationStatus.REQUESTED:
        return False
    assert incarnation is not None  # noqa: S101 — native_status requires it
    if session["status"] == ImpersonationStatus.ACCEPTED:
        if not activate_accepted:
            return False
        activated = await _activate_accepted(
            graph,
            db,
            bus,
            session,
            incarnation,
            relays,
            notes=notes,
            read_local_machine=read_local_machine,
        )
        if activated is None:
            return False
        session = activated
    if session["status"] == ImpersonationStatus.ACTIVE:
        return True
    await _apply_plugin_deltas(graph, db, session, agent_id, incarnation)
    if session["automatic"] and session["handoff_applied_at"] is None:
        from base.agents.impersonation import aborted_detail

        # A supervisor-aborted lease (task #3998) carries its death cause as
        # "aborted: <detail>" in rejection_reason; the end note names it.
        await _handoff.deliver_handoff(
            graph,
            db,
            bus,
            session,
            incarnation,
            notes=notes,
            reason=aborted_detail(session.get("rejection_reason")),
        )
    return False


# ── Bound relay process: establishment, supervision, teardown ─────────────────

RELAY_READY_TIMEOUT_S = 30.0
_RELAY_READY_POLL_S = 1.0


def establish_relay(
    db: Database,
    bus: EventBus,
    session: dict[str, Any],
    incarnation: RuntimeIncarnation,
    relays: RelaySupervision,
    *,
    read_local_machine: Callable[[], str],
) -> bool:
    """Activation gate: the bound relay must be up before the takeover stands.

    codex — provision the scoped credential, spawn the relay here, and wait for
    its first heartbeat. claude / dsh — the relay runs inside the controller's
    own session; require a fresh heartbeat instead. Any failure rolls the lease to
    'rejected' with a loud reason (fail_acceptance) and returns False; native
    control resumes. A lease that is no longer 'accepted' returns False without
    a transition — someone else already ended it.
    """
    from base.agents.impersonation import SESSION_RELAY_PROVIDERS, fail_acceptance

    provider = session["relay_provider"]
    if provider in SESSION_RELAY_PROVIDERS:
        return _await_session_relay(db, bus, session, incarnation)
    if provider not in ("codex", "antigravity"):
        # Defensive: accept() already rejects leases without a relay binding.
        return _roll_back_relay_failure(
            db, bus, session, incarnation, fail_acceptance, "request has no relay binding"
        )
    return _establish_codex_relay(
        db, bus, session, incarnation, relays, read_local_machine=read_local_machine
    )


def _await_session_relay(
    db: Database, bus: EventBus, session: dict[str, Any], incarnation: RuntimeIncarnation
) -> bool:
    """claude / dsh: the relay runs in the controller's own session; require a fresh heartbeat."""
    from base.agents.impersonation import fail_acceptance

    if session["automatic"]:
        from base.agents.impersonation import native_status as read_status

        deadline = time.monotonic() + RELAY_READY_TIMEOUT_S
        while not _heartbeat_fresh(session["relay_heartbeat_at"]) and time.monotonic() < deadline:
            time.sleep(_RELAY_READY_POLL_S)
            latest = read_status(db, bus, incarnation.agent_id, incarnation)
            if (
                latest is None
                or latest["id"] != session["id"]
                or latest["status"] != ImpersonationStatus.ACCEPTED
            ):
                return False
            session = latest
    if _heartbeat_fresh(session["relay_heartbeat_at"]):
        return True
    return _roll_back_relay_failure(
        db,
        bus,
        session,
        incarnation,
        fail_acceptance,
        "the controller relay is not running (no fresh heartbeat)",
    )


def _establish_codex_relay(
    db: Database,
    bus: EventBus,
    session: dict[str, Any],
    incarnation: RuntimeIncarnation,
    relays: RelaySupervision,
    *,
    read_local_machine: Callable[[], str],
) -> bool:
    """codex / antigravity: provision the scoped credential, spawn the relay here, wait for its first heartbeat."""
    from base.agents.impersonation import (
        ImpersonationError,
        fail_acceptance,
        provision_relay,
        relay_get,
    )

    # meiva:note
    # Resolve the Antigravity destination here, before a relay exists: a relay that
    # dies at startup leaves only an exit code in the lease's reason, and the path or
    # token problem is exactly what the accepting runtime can name while it still has
    # the record in hand. The cost is one owner-only file read plus at most one probe.
    # meiva:link docs/conventions/agents/agent-impersonation-hosts.md
    antigravity_endpoint_file = None
    if session["relay_provider"] == "antigravity":
        from base.agents.impersonation.host_transport import (
            antigravity_endpoint_path,
            resolve_antigravity_endpoint,
        )

        try:
            resolve_antigravity_endpoint(session["relay_thread_id"])
        except (OSError, RuntimeError, ValueError) as exc:
            return _roll_back_relay_failure(
                db,
                bus,
                session,
                incarnation,
                fail_acceptance,
                f"the Antigravity endpoint is unusable: {exc}",
            )
        antigravity_endpoint_file = str(antigravity_endpoint_path(session["relay_thread_id"]))

    relay_token = secrets.token_urlsafe(32)
    try:
        minted = provision_relay(db, session["id"], incarnation, relay_token)
        if minted is None:
            return False
    except ImpersonationError:
        return False  # the lease is no longer native-held accepted state
    child = RelayChild(
        session["id"],
        _relay_process.spawn_codex_relay(
            incarnation.agent_id,
            session["id"],
            relay_token,
            session["relay_thread_id"],
            session["relay_codex_remote"],
            session["process_metadata"].get("codex_home"),
            provider=session["relay_provider"],
            antigravity_endpoint_file=antigravity_endpoint_file,
        ),
        relay_token,
        time.monotonic(),
        minted["relay_generation"],
    )
    relays.children[incarnation.agent_id] = child
    deadline = time.monotonic() + RELAY_READY_TIMEOUT_S
    while time.monotonic() < deadline:
        if child.process.poll() is not None:
            relays.children.pop(incarnation.agent_id, None)
            return _roll_back_relay_failure(
                db,
                bus,
                session,
                incarnation,
                fail_acceptance,
                f"{session['relay_provider']} relay exited during startup"
                f" (exit code {child.process.returncode}); its transport error is on stderr",
            )
        try:
            latest = relay_get(
                db, bus, session["id"], relay_token, local_machine=read_local_machine()
            )
        except ImpersonationError:
            # Token mismatch means a re-provision raced in; treat as ended here.
            return _roll_back_relay_failure(
                db,
                bus,
                session,
                incarnation,
                fail_acceptance,
                "relay credential was re-provisioned",
            )
        if latest["status"] != ImpersonationStatus.ACCEPTED:
            return False  # released/expired/rejected while starting; no relay needed
        if _heartbeat_fresh(latest["relay_heartbeat_at"]):
            return True
        time.sleep(_RELAY_READY_POLL_S)
    terminate_relay(child)
    relays.children.pop(incarnation.agent_id, None)
    return _roll_back_relay_failure(
        db, bus, session, incarnation, fail_acceptance, "relay did not become ready in time"
    )


def _roll_back_relay_failure(
    db: Database,
    bus: EventBus,
    session: dict[str, Any],
    incarnation: RuntimeIncarnation,
    fail_acceptance: Callable[[Database, EventBus, str, RuntimeIncarnation, str], dict[str, Any]],
    reason: str,
) -> bool:
    from base.agents.impersonation import ImpersonationError

    with contextlib.suppress(ImpersonationError):
        fail_acceptance(db, bus, session["id"], incarnation, reason)  # already ended: no-op
    return False


# meiva:note
# Observe relay delivery, end takeovers whose relay died, retire the children. An
# exited relay this host spawned ends its takeover: nothing can push inbound
# messages any more, so control returns to the native agent instead of parking them
# until the TTL. A relay that is only stale keeps the lease open until its TTL or
# explicit release, because another host may still deliver. When the host first sees
# its relay's lease ended, it submits the ended-lease notice once.
# meiva:link docs/conventions/agents/agent-impersonation-hosts.md
async def supervise_relay(
    db: Database,
    bus: EventBus,
    session: dict[str, Any] | None,
    agent_id: int,
    relays: RelaySupervision,
    *,
    incarnation: RuntimeIncarnation | None,
) -> None:
    child = relays.children.get(agent_id)
    if (
        child is not None
        and session is not None
        and (child.lease_id, child.generation) != (session["id"], session["relay_generation"])
    ):
        return  # A delayed predecessor snapshot cannot retire a replacement's sender.
    if session is None or session["status"] not in OPEN:
        if child is not None and _lease_is_terminal(db, child.lease_id):
            await _retire_relay_child(db, agent_id, relays, child)
        return
    if session["status"] != ImpersonationStatus.ACTIVE:
        return
    exited = relay_exited(child)
    if not exited and _heartbeat_fresh(session["relay_heartbeat_at"]):
        return
    if exited and child is not None:
        ended = await asyncio.to_thread(
            _end_dead_relay_lease, db, bus, session, agent_id, incarnation, child.process.returncode
        )
        if ended:
            await _retire_relay_child(db, agent_id, relays, child)
        return
    await _record_stale_relay(db, session, agent_id, child, incarnation=incarnation)


def _lease_is_terminal(db: Database, lease_id: str) -> bool:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT status FROM agent_impersonations WHERE id=%s", (lease_id,)
        ).fetchone()
    return row is not None and ImpersonationStatus(row[0]) not in OPEN


async def _retire_relay_child(
    db: Database, agent_id: int, relays: RelaySupervision, child: RelayChild
) -> None:
    if relays.children.get(agent_id) is not child:
        return
    relays.children.pop(agent_id, None)
    await asyncio.to_thread(terminate_relay, child)
    await _send_ended_notice(db, agent_id, child.lease_id)


# meiva:note
# End a takeover whose own relay child is confirmed gone. A poll result on the
# child this host spawned is a deterministic death, not a liveness gap another
# host could still cover, so nothing can push inbound messages any more. Ending
# the lease returns control to the native agent, which answers the messages that
# would otherwise wait for the TTL. A normal exit, a signal and a failed start
# are the same fact here; the exit code only names how it died.
# meiva:link docs/conventions/agents/agent-impersonation-hosts.md
def _end_dead_relay_lease(
    db: Database,
    bus: EventBus,
    session: dict[str, Any],
    agent_id: int,
    incarnation: RuntimeIncarnation | None,
    exit_code: int | None,
) -> bool:
    from base.agents.impersonation import ImpersonationError, abort_lease
    from base.log import logger

    if incarnation is None:
        return False
    detail = f"the bound relay process exited (exit code {exit_code})"
    try:
        ended = abort_lease(db, bus, session["id"], incarnation, detail)
    except (ImpersonationError, RuntimeError):
        return False
    if ended is None:
        return False
    logger.error(
        "impersonation relay exited; the takeover ended and native control returned",
        agent_id=agent_id,
        lease_id=str(session["id"]),
        relay_provider=session["relay_provider"],
        exit_code=exit_code,
    )
    return True


# meiva:note
# Record a stale relay as degraded; nothing replaces it. Only a missing
# heartbeat reaches here: an exited child this host spawned ends its lease
# instead. A relay still inside its startup window is left alone. The lease keeps
# its original TTL, so messages stay in the inbox and the liveness alert names the
# gap until the controller re-requests or releases.
# meiva:link docs/conventions/agents/agent-impersonation-hosts.md
async def _record_stale_relay(
    db: Database,
    session: dict[str, Any],
    agent_id: int,
    child: RelayChild | None,
    *,
    incarnation: RuntimeIncarnation | None,
) -> None:
    from base.agents.impersonation.relay import record_degradation

    if incarnation is None:
        return
    if (
        child is not None
        and child.process.poll() is None
        and time.monotonic() - child.spawned_at < RELAY_READY_TIMEOUT_S
    ):
        return
    minted_at = session.get("relay_minted_at")
    if (
        minted_at is not None
        and (datetime.now(UTC) - minted_at).total_seconds() < RELAY_READY_TIMEOUT_S
    ):
        return
    await asyncio.to_thread(
        record_degradation,
        db,
        session["id"],
        incarnation,
        "relay unavailable; automatic recovery unsupported",
    )
    await asyncio.to_thread(
        stamp_relay_failure,
        db,
        session,
        agent_id,
        record_relay_failure,
        incarnation=incarnation,
    )


async def _send_ended_notice(db: Database, agent_id: int, lease_id: str) -> None:
    """Submit the ended lease's notice once; a failure is logged and never retried."""
    from base.agents.impersonation.terminal_notices import deliver_ended_notice

    await asyncio.to_thread(deliver_ended_notice, db, agent_id, lease_id)
