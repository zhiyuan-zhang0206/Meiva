"""Cooperative takeover at the native runtime's durable invocation boundary.

The external holder never writes a graph checkpoint. Accepted requests stop
native nodes; only the invocation driver activates after exec closure and the
checkpointer flush. A database lease gates every subsequent invocation.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import random
import sys
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.types import Command
from psycopg_pool import AsyncConnectionPool

from agent import impersonation_handoff as _handoff
from agent import state as _state
from agent.nodes import END, NodeName
from agent.takeover.gate import (
    GateVerdict,
    LeasePhase,
    SettleStep,
    delta_plan,
    gate_verdict,
    handoff_due,
    settle_step,
    start_brief,
)
from agent.takeover.process import spawn_relay
from agent.takeover.relay import (
    ADOPTED_RELAY_DOWN_REASON,
    CREDENTIAL_RENEWED_REASON,
    NOT_READY_REASON,
    SESSION_RELAY_DOWN_REASON,
    STALE_RELAY_REASON,
    UNBOUND_REASON,
    ChildView,
    LeaseView,
    Poll,
    RelayBinding,
    RelayRoute,
    RelayWait,
    Supervision,
    TakeoverHost,
    endpoint_unusable_reason,
    relay_poll,
    relay_route,
    startup_failure,
    supervision_step,
)
from base.agents.context import MeivaContext, agent_id_from_config
from base.agents.impersonation import (
    OPEN,
    SESSION_RELAY_PROVIDERS,
    ImpersonationError,
    abort_lease,
    aborted_detail,
    activate,
    fail_acceptance,
    mark_plugin_applied,
    prepare,
    provision_relay,
    relay_get,
)
from base.agents.impersonation import native_status as read_status
from base.agents.impersonation.relay import (
    RelayAction,
    record_degradation,
    stamp_relay_failure,
)
from base.agents.impersonation.status import ImpersonationStatus
from base.agents.messages.timestamp_policy import TimestampSource
from base.agents.observation.relay_supervision import RelayChild, RelaySupervision, relay_exited
from base.agents.observation.relay_supervision import heartbeat_fresh as _heartbeat_fresh
from base.agents.observation.relay_supervision import terminate_relay as terminate_relay
from base.clock import Clock
from base.db import Database
from base.db.protocols import DatabaseLike
from base.db.transaction import async_write_transaction
from base.events.live.bus import EventBus
from base.log import logger
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.paths import workspaces_root

RELAY_WAIT = RelayWait()


def process_host(
    local_machine: str,
    *,
    home: Path,
    environ: Mapping[str, str],
    rng: random.Random,
    clock: Clock,
) -> TakeoverHost:
    return TakeoverHost(
        local_machine=local_machine,
        home=home,
        workspaces_root=workspaces_root(home=home),
        relay_program=(str(Path(sys.executable).with_name("meiva")),),
        environ=environ,
        rng=rng,
        clock=clock,
    )


# meiva:note
# The credential a spawned relay presents to the lease: 32 random bytes as URL-safe base64 without
# padding (43 characters). It is a secret, so the host's generator must be a secure one.
def draw_relay_token(rng: random.Random) -> str:
    return base64.urlsafe_b64encode(rng.randbytes(32)).rstrip(b"=").decode("ascii")


def _held(
    session: dict[str, Any] | None, incarnation: RuntimeIncarnation | None
) -> tuple[dict[str, Any], RuntimeIncarnation]:
    if session is None or incarnation is None:
        raise RuntimeError("a lease decision needs the admitted native runtime that read it")
    return session, incarnation


def lease_phase(session: dict[str, Any] | None) -> LeasePhase | None:
    if session is None:
        return None
    match session["status"]:
        case ImpersonationStatus.REQUESTED:
            return LeasePhase.REQUESTED
        case ImpersonationStatus.ACCEPTED:
            return LeasePhase.ACCEPTED
        case ImpersonationStatus.ACTIVE:
            return LeasePhase.ACTIVE
        case (
            ImpersonationStatus.RELEASED
            | ImpersonationStatus.REJECTED
            | ImpersonationStatus.EXPIRED
        ):
            return LeasePhase.ENDED
        case unknown:
            raise ValueError(f"unknown impersonation lease status: {unknown!r}")


async def native_status(
    db: DatabaseLike, bus: EventBus, agent_id: int, *, incarnation: RuntimeIncarnation | None
) -> dict[str, Any] | None:
    """Read authoritative lease state only for an admitted native runtime."""
    if incarnation is None:
        return None
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
    # meiva:link agent/docs/impersonation/impersonation.meiva.okf.md
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
        clock=ctx.require_clock(),
        home=ctx.require_meiva_home(),
    )
    verdict = gate_verdict(lease_phase(session))
    if verdict is GateVerdict.PROCEED:
        return None
    if verdict is GateVerdict.PREPARE:
        session, incarnation = _held(session, incarnation)
        brief = start_brief(session["reason"])
        await asyncio.to_thread(
            prepare,
            db,
            bus,
            session["id"],
            agent_id,
            incarnation,
            brief,
            local_machine=ctx.require_machine().name(),
        )
        stamp = _handoff.note_stamp(
            ctx.require_clock(),
            timestamps=ctx.require_agent().read("general", "message_timestamps"),
        )
        return Command(
            update={
                **_handoff.start_update(
                    session, introduced=state.impersonation_introduced, stamp=stamp
                ),
                "turn_active": False,
                "turn_idle": True,
            },
            goto=END,
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
            if gate_verdict(lease_phase(session)) is GateVerdict.PARK:
                return Command(update={"turn_idle": True}, goto=END)
        return await runner(state, runtime, config)

    return guarded


async def _activate_accepted(
    graph: _handoff.AgentGraph,
    db: DatabaseLike,
    bus: EventBus,
    session: dict[str, Any],
    incarnation: RuntimeIncarnation,
    relays: RelaySupervision,
    *,
    timestamps: TimestampSource,
    host: TakeoverHost,
) -> dict[str, Any] | None:
    """Activate an accepted lease; None when the relay gate failed and native control resumes."""
    await _handoff.ensure_start_marker(graph, session, notes=timestamps.policy())
    # The bound relay must be live before the takeover stands. On failure
    # the lease is rolled back to 'rejected' with a loud reason and the
    # native agent resumes — no silent half-takeover.
    if not await asyncio.to_thread(
        establish_relay, db, bus, session, incarnation, relays, host=host
    ):
        return None
    return await asyncio.to_thread(activate, db, bus, session["id"], incarnation)


# meiva:note
# Apply each unapplied plugin delta exactly once, recording its receipt.
async def apply_plugin_deltas(
    graph: _handoff.AgentGraph,
    db: DatabaseLike,
    session: dict[str, Any],
    agent_id: int,
    incarnation: RuntimeIncarnation,
) -> None:
    from base.agents.context.external_attachment.plugin_state_codec import decode_plugin_delta

    config: RunnableConfig = {"configurable": {"thread_id": str(agent_id)}}
    snapshot = await graph.aget_state(config)
    plan = delta_plan(
        lease_id=session["id"],
        applied_version=session["applied_version"],
        delta_version=session["delta_version"],
        receipt=snapshot.values.get("impersonation_applied", {}),
    )
    for step in plan:
        if step.apply:
            delta = decode_plugin_delta(
                session["plugin_delta"][step.version - 1], graph.builder.state_schema
            )
            receipt = {"lease_id": session["id"], "version": step.version}
            await graph.aupdate_state(config, {**delta, "impersonation_applied": receipt})
            await _handoff.flush_graph_checkpoint(graph, agent_id)
        await asyncio.to_thread(mark_plugin_applied, db, session["id"], step.version, incarnation)


# meiva:note
# A read-only answer to "did the receipt land", for the wake whose budget ran out after the
# write but before the settle returned (#5468). No lock and no expiry write: the caller wants a
# yes/no about the one fact it may have just committed, and nothing is decided here. "Owed" is
# the same fact `handoff_due(ENDED, receipt_written=False)` names — an ended lease with no
# receipt — written out in SQL here because that helper reads a parsed row, not a table.
# meiva:link docs/conventions/agents/agent-impersonation.md
async def handoff_landed(pool: AsyncConnectionPool, agent_id: int) -> bool:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT EXISTS (SELECT 1 FROM agent_impersonations "
            "WHERE agent_id = %s AND status IN ('released','rejected','expired') "
            "AND handoff_applied_at IS NULL)",
            (agent_id,),
        )
        row = await cur.fetchone()
    if row is None:
        raise RuntimeError("an EXISTS query always returns one row")
    return not row[0]


# meiva:note
# The marker write the held path owns (#5468). A settle that failed inside the wake stamps the
# crash marker; a settle that then writes the receipt must erase it, or the row would look
# crash-dead forever — and the held path runs no model turn, so nothing would ever complete a
# turn to clear it.
#
# The condition is the fact that settles the question, not the marker's timestamp: the row must
# have no ended lease still owing a receipt. A timestamp test (`marker >= last_admission_at`)
# reads well and strands the common sequence: a COALESCE stamp keeps the FIRST death, so a stamp
# from an earlier wake is older than any later admission, and the marker it left would stay on a
# row this wake settled — fifteen minutes later the reaper would terminate a healthy agent. The
# cost of the fact-based rule, stated plainly: a marker left by a genuine turn crash is also
# cleared once this row's receipt is settled, so that crash is retried on the next input instead
# of being reaped. The incarnation CAS stays: another runtime's marker is not ours to clear.
#
# What this narrows, stated where the next reader will look: after the clear the row's recovery
# depends on the ordinary heartbeat. A row with `permanent_reject_streak > 0` or a heartbeat
# paused past now takes no heartbeat at all, so it has no recovery path here — unlike before
# this change, where the leftover marker still let the reaper take it. The clear logs that
# combination (`hosted_handoff_marker_cleared_while_ineligible`) so it is visible in the log
# rather than only in this note.
# meiva:link docs/conventions/agents/agent-impersonation.md
async def clear_turn_fatal(pool: AsyncConnectionPool, incarnation: RuntimeIncarnation) -> bool:
    async with async_write_transaction(pool) as conn:
        cur = await conn.execute(
            "UPDATE agents_meta SET last_turn_fatal_at = NULL "
            "WHERE id = %s AND runtime_generation = %s AND runtime_owner = %s "
            "AND last_turn_fatal_at IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM agent_impersonations owed "
            "  WHERE owed.agent_id = agents_meta.id "
            "    AND owed.status IN ('released','rejected','expired') "
            "    AND owed.handoff_applied_at IS NULL) "
            "RETURNING permanent_reject_streak, heartbeat_paused_until > now()",
            (incarnation.agent_id, incarnation.generation, incarnation.owner),
        )
        row = await cur.fetchone()
    if row is None:
        return False
    streak, paused = row
    logger.info(
        "a crash marker cleared by the wake that settled the row",
        event="hosted_turn_corpse_marker_cleared",
        agent_id=incarnation.agent_id,
    )
    if streak > 0 or paused:
        logger.warning(
            "cleared a marker on a row the heartbeat will not pick up",
            event="hosted_handoff_marker_cleared_while_ineligible",
            agent_id=incarnation.agent_id,
            permanent_reject_streak=streak,
            heartbeat_paused=bool(paused),
        )
    return True


def handoff_owed(session: dict[str, Any] | None) -> bool:
    phase = lease_phase(session)
    return (
        session is not None
        and phase is not None
        and handoff_due(phase, receipt_written=session["handoff_applied_at"] is not None)
    )


async def settle_checkpoint(
    graph: _handoff.AgentGraph,
    db: DatabaseLike,
    bus: EventBus,
    agent_id: int,
    relays: RelaySupervision,
    *,
    activate_accepted: bool = True,
    incarnation: RuntimeIncarnation | None,
    timestamps: TimestampSource,
    host: TakeoverHost,
) -> bool:
    """After invocation+flush, activate; apply terminal deltas exactly once."""
    session = await native_status(db, bus, agent_id, incarnation=incarnation)
    step = settle_step(lease_phase(session), activate_accepted=activate_accepted)
    if step is SettleStep.STOP:
        return False
    session, incarnation = _held(session, incarnation)
    if step is SettleStep.ACTIVATE:
        activated = await _activate_accepted(
            graph, db, bus, session, incarnation, relays, timestamps=timestamps, host=host
        )
        if activated is None:
            return False
        session = activated
        step = settle_step(lease_phase(session), activate_accepted=False)
    if step is SettleStep.HOLD:
        return True
    await apply_plugin_deltas(graph, db, session, agent_id, incarnation)
    if handoff_owed(session):
        # A supervisor-aborted lease (task #3998) carries its death cause as
        # "aborted: <detail>" in rejection_reason; the end note names it.
        await _handoff.deliver_handoff(
            graph,
            db,
            bus,
            session,
            incarnation,
            timestamps=timestamps,
            workspace=host.workspace(agent_id),
            reason=aborted_detail(session.get("rejection_reason")),
        )
    return False


# ── Bound relay process: establishment, supervision, teardown ─────────────────


def establish_relay(
    db: DatabaseLike,
    bus: EventBus,
    session: dict[str, Any],
    incarnation: RuntimeIncarnation,
    relays: RelaySupervision,
    *,
    host: TakeoverHost,
) -> bool:
    """Activation gate: the bound relay must be up before the takeover stands.

    codex — provision the scoped credential, spawn the relay here, and wait for
    its first heartbeat. claude / dsh — the relay runs inside the controller's
    own session; require a fresh heartbeat instead. Any failure rolls the lease to
    'rejected' with a loud reason (fail_acceptance) and returns False; native
    control resumes. A lease that is no longer 'accepted' returns False without
    a transition — someone else already ended it.
    """
    route = relay_route(session["relay_provider"], session_providers=SESSION_RELAY_PROVIDERS)
    if route is RelayRoute.SESSION:
        return _await_existing_relay(
            db, bus, session, incarnation, host, reason=SESSION_RELAY_DOWN_REASON
        )
    if route is RelayRoute.UNBOUND:
        return _roll_back_relay_failure(db, bus, session, incarnation, UNBOUND_REASON)
    return _establish_spawned_relay(db, bus, session, incarnation, relays, host)


def _await_existing_relay(
    db: DatabaseLike,
    bus: EventBus,
    session: dict[str, Any],
    incarnation: RuntimeIncarnation,
    host: TakeoverHost,
    *,
    reason: str,
) -> bool:
    # meiva:note
    # Wait for a carrier this host did not spawn; it must beat before the takeover stands.
    #
    # Two callers share it: the session relays (claude, dsh), whose carrier runs inside the
    # controller's own session, and an adopted spawned relay, whose carrier the predecessor
    # spawned outside this host (`start_new_session`) and which therefore outlived it. This
    # host holds no child handle in the second case and never terminates it: only the lease's
    # end does that, from the carrier's own side.
    clock, wait = host.clock, host.wait
    started = clock.monotonic()
    phase = lease_phase(session)
    while True:
        poll = relay_poll(
            phase,
            heartbeat_fresh=_heartbeat_fresh(session["relay_heartbeat_at"], now=clock.now()),
            waited_s=clock.monotonic() - started,
            wait=wait,
        )
        if poll is Poll.READY:
            return True
        if poll is Poll.ABANDON:
            return False
        if poll is Poll.GIVE_UP:
            return _roll_back_relay_failure(db, bus, session, incarnation, reason)
        clock.sleep(wait.poll_s)
        latest = read_status(db, bus, incarnation.agent_id, incarnation)
        if latest is None or latest["id"] != session["id"]:
            phase = None
        else:
            phase, session = lease_phase(latest), latest


def _establish_spawned_relay(
    db: DatabaseLike,
    bus: EventBus,
    session: dict[str, Any],
    incarnation: RuntimeIncarnation,
    relays: RelaySupervision,
    host: TakeoverHost,
) -> bool:
    from base.agents.impersonation.host_transport import (
        antigravity_endpoint_path,
        resolve_antigravity_endpoint,
    )

    # meiva:note
    # codex / antigravity: provision the scoped credential, spawn the relay here and wait
    # for its first heartbeat. Resolve the Antigravity destination first, before a relay
    # exists: a relay that dies at startup leaves only an exit code in the lease's reason,
    # and the path or token problem is exactly what the accepting runtime can name while
    # it still has the record in hand. The cost is one owner-only file read plus at most
    # one probe.
    # meiva:link docs/conventions/agents/agent-impersonation-hosts.md
    antigravity_endpoint_file = None
    if session["relay_provider"] == "antigravity":
        try:
            resolve_antigravity_endpoint(
                session["relay_thread_id"], home=host.home, antigravity_server=None
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return _roll_back_relay_failure(
                db, bus, session, incarnation, endpoint_unusable_reason(str(exc))
            )
        antigravity_endpoint_file = str(
            antigravity_endpoint_path(session["relay_thread_id"], home=host.home)
        )

    relay_token = draw_relay_token(host.rng)
    try:
        provision = provision_relay(db, session["id"], incarnation, relay_token)
    except ImpersonationError:
        return False
    if provision is None:
        return False
    if provision.action is RelayAction.ADOPTED:
        # meiva:note
        # The predecessor's carrier is still beating; the credential the external side
        # already holds stays valid, so this host spawns nothing and only waits for it.
        return _await_existing_relay(
            db, bus, session, incarnation, host, reason=ADOPTED_RELAY_DOWN_REASON
        )
    binding = RelayBinding(
        agent_id=incarnation.agent_id,
        lease_id=session["id"],
        provider=session["relay_provider"],
        thread_id=session["relay_thread_id"],
        codex_remote=session["relay_codex_remote"],
        antigravity_endpoint_file=antigravity_endpoint_file,
        codex_home=session["process_metadata"].get("codex_home"),
    )
    child = RelayChild(
        session["id"],
        spawn_relay(host.relay_program, binding, token=relay_token, environ=host.environ),
        relay_token,
        host.clock.monotonic(),
        provision.lease["relay_generation"],
    )
    relays.children[incarnation.agent_id] = child
    return _await_spawned_relay(db, bus, session, incarnation, relays, child, host)


def _await_spawned_relay(
    db: DatabaseLike,
    bus: EventBus,
    session: dict[str, Any],
    incarnation: RuntimeIncarnation,
    relays: RelaySupervision,
    child: RelayChild,
    host: TakeoverHost,
) -> bool:
    clock = host.clock
    started = clock.monotonic()
    while True:
        failure = startup_failure(session["relay_provider"], child.process.poll())
        if failure is not None:
            relays.children.pop(incarnation.agent_id, None)
            return _roll_back_relay_failure(db, bus, session, incarnation, failure)
        try:
            latest = relay_get(
                db, bus, session["id"], child.token, local_machine=host.local_machine
            )
        except ImpersonationError:
            return _roll_back_relay_failure(
                db, bus, session, incarnation, CREDENTIAL_RENEWED_REASON
            )
        poll = relay_poll(
            lease_phase(latest),
            heartbeat_fresh=_heartbeat_fresh(latest["relay_heartbeat_at"], now=clock.now()),
            waited_s=clock.monotonic() - started,
            wait=host.wait,
        )
        if poll is Poll.READY:
            return True
        if poll is Poll.ABANDON:
            return False
        if poll is Poll.GIVE_UP:
            terminate_relay(child)
            relays.children.pop(incarnation.agent_id, None)
            return _roll_back_relay_failure(db, bus, session, incarnation, NOT_READY_REASON)
        clock.sleep(host.wait.poll_s)


def _roll_back_relay_failure(
    db: DatabaseLike,
    bus: EventBus,
    session: dict[str, Any],
    incarnation: RuntimeIncarnation,
    reason: str,
) -> bool:
    with contextlib.suppress(ImpersonationError):
        fail_acceptance(db, bus, session["id"], incarnation, reason)
    return False


def _lease_view(session: dict[str, Any], *, now: datetime) -> LeaseView | None:
    phase = lease_phase(session)
    if phase is None:
        return None
    minted_at = session.get("relay_minted_at")
    return LeaseView(
        lease_id=session["id"],
        generation=session["relay_generation"],
        phase=phase,
        heartbeat_fresh=_heartbeat_fresh(session["relay_heartbeat_at"], now=now),
        minted_age_s=None if minted_at is None else (now - minted_at).total_seconds(),
    )


# meiva:note
# Observe relay delivery, end takeovers whose relay died, retire the children. An
# exited relay this host spawned ends its takeover: nothing can push inbound
# messages any more, so control returns to the native agent instead of parking them
# until the TTL. A relay that is only stale keeps the lease open until its TTL or
# explicit release, because another host may still deliver. When the host first sees
# its relay's lease ended, it submits the ended-lease notice once.
# meiva:link agent/docs/impersonation/relay-supervision.meiva.okf.md
async def supervise_relay(
    db: Database,
    bus: EventBus,
    session: dict[str, Any] | None,
    agent_id: int,
    relays: RelaySupervision,
    *,
    incarnation: RuntimeIncarnation | None,
    clock: Clock,
    home: Path,
) -> None:
    child = relays.children.get(agent_id)
    now, monotonic = clock.now(), clock.monotonic()
    step = supervision_step(
        None
        if child is None
        else ChildView(
            lease_id=child.lease_id,
            generation=child.generation,
            exited=relay_exited(child),
            age_s=monotonic - child.spawned_at,
        ),
        None if session is None else _lease_view(session, now=now),
        native_held=incarnation is not None,
        wait=RELAY_WAIT,
    )
    if step is Supervision.IDLE:
        return
    if step is Supervision.RETIRE_IF_ENDED:
        if child is not None and _lease_is_terminal(db, child.lease_id):
            await _retire_relay_child(db, agent_id, relays, child, home=home)
        return
    session, incarnation = _held(session, incarnation)
    if step is Supervision.END_DEAD and child is not None:
        ended = await asyncio.to_thread(
            _end_dead_relay_lease, db, bus, session, agent_id, incarnation, child.process.returncode
        )
        if ended:
            await _retire_relay_child(db, agent_id, relays, child, home=home)
        return
    await _record_stale_relay(db, session, agent_id, incarnation)


def _lease_is_terminal(db: Database, lease_id: str) -> bool:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT status FROM agent_impersonations WHERE id=%s", (lease_id,)
        ).fetchone()
    return row is not None and ImpersonationStatus(row[0]) not in OPEN


async def _retire_relay_child(
    db: Database, agent_id: int, relays: RelaySupervision, child: RelayChild, *, home: Path
) -> None:
    if relays.children.get(agent_id) is not child:
        return
    relays.children.pop(agent_id, None)
    await asyncio.to_thread(terminate_relay, child)
    await _send_ended_notice(db, agent_id, child.lease_id, home=home)


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
    incarnation: RuntimeIncarnation,
    exit_code: int | None,
) -> bool:
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
# Record a stale relay as degraded; nothing replaces it. Only a missing heartbeat
# outside the startup window reaches here: an exited child this host spawned ends its
# lease instead, and the supervision plan leaves a relay still inside its startup
# window alone. The lease keeps its original TTL, so messages stay in the inbox and
# the liveness alert names the gap until the controller re-requests or releases.
# meiva:link docs/conventions/agents/agent-impersonation-hosts.md
async def _record_stale_relay(
    db: Database, session: dict[str, Any], agent_id: int, incarnation: RuntimeIncarnation
) -> None:
    await asyncio.to_thread(
        record_degradation,
        db,
        session["id"],
        incarnation,
        STALE_RELAY_REASON,
    )
    await asyncio.to_thread(
        stamp_relay_failure,
        db,
        session,
        agent_id,
        incarnation=incarnation,
    )


async def _send_ended_notice(db: Database, agent_id: int, lease_id: str, *, home: Path) -> None:
    """Submit the ended lease's notice once; a failure is logged and never retried."""
    from base.agents.impersonation.terminal_notices import deliver_ended_notice

    # meiva:note
    # The notice resolves its Antigravity destination from a record under this host's
    # home, so the home travels down from the host that owns the process rather than
    # being looked up inside the notice module.
    await asyncio.to_thread(deliver_ended_notice, db, agent_id, lease_id, home=home)
