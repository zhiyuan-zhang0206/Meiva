# meiva:note
# Push a durable inbox through the bound relay, without ACK or lease renewal.
#
# Each message uses the delivery budget and ACK window snapshotted on its lease. Reservations
# survive relay restarts. Exhaustion pauses that message's automatic push; its durable body
# remains readable and late receipt ACKs remain valid. Executor authority keeps its original
# TTL.
#
# This module is the shell: it reads the inbox, reserves the delivery in the database,
# delivers into the host and listens for wakes. Every decision between those effects is a
# function in `relay/`.
# meiva:link docs/conventions/agents/agent-impersonation-hosts.md

from __future__ import annotations

import argparse
import asyncio
import contextlib
import random
import subprocess
import sys
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TextIO, assert_never
from uuid import UUID

from pydantic import BaseModel

from base.agents.context.clients import DatabaseFactory
from base.agents.impersonation import (
    RELAY_HEARTBEAT_SECONDS,
    ImpersonationError,
    relay_get,
    relay_heartbeat,
)
from base.agents.impersonation import relay_inbox as read_inbox_rows
from base.agents.impersonation.delivery import reserve_delivery
from base.agents.impersonation.history import resolve
from base.agents.impersonation.host_transport import AntigravityServer
from base.agents.impersonation.status import OPEN, ImpersonationStatus
from base.agents.impersonation.terminal_notices import notice_text
from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from base.cluster.machine import MachineIdentityReader
from base.db import Database
from base.events.live.bus import EventBus
from base.events.live.redis_listener import RedisInboundListener
from cli.commands.agents.cleanup_after import run_then_clean_up
from cli.commands.agents.impersonation_adapters import (
    RelayAdapter,
    bind_adapter,
    deliver,
    notifies_terminal,
)
from cli.commands.agents.relay.destination import plan_destination
from cli.commands.agents.relay.inbox import (
    DeliveryBudget,
    EndedLease,
    EndVerdict,
    InboxMessage,
    InboxSnapshot,
    LeaseRef,
    end_verdict,
    routine_ids,
    terminal_record,
)
from cli.commands.agents.relay.steps import (
    PUSH_MAX_CHARS,
    ClockReading,
    Deliver,
    RelayState,
    RelayTuning,
    SendStart,
    WaitForWake,
    batch_closed,
    emitted,
    next_step,
    plan_reservation,
    reserved_push,
)


# meiva:note
# The lease as the relay's credential reads it. A status the shared enum does not know fails
# here, at the wire, rather than being treated as one it does.
class Lease(BaseModel):
    id: UUID
    agent_id: int
    status: ImpersonationStatus
    expires_at: datetime
    ack_window_seconds: int
    max_delivery_attempts: int
    relay_batch_window_seconds: int = 0
    start_message: str = ""
    rejection_reason: str | None = None
    terminal_notice_snapshot: EndedLease | None = None

    def budget(self) -> DeliveryBudget:
        return DeliveryBudget(self.ack_window_seconds, self.max_delivery_attempts)


# meiva:note
# The lease one relay serves and the credential it serves it with: the agent, the lease's
# private id (database reads and reservations), the session number the ACK command names, the
# scoped relay token, and the machine the lease must be placed on.
@dataclass(frozen=True)
class RelayLease:
    agent_id: int
    lease_id: UUID
    session_id: int
    token: str
    machine: str

    def ref(self) -> LeaseRef:
        return LeaseRef(self.agent_id, self.session_id)


def _lease(db: Database, bus: EventBus, lease_id: UUID, token: str, machine: str) -> Lease:
    return Lease.model_validate(relay_get(db, bus, str(lease_id), token, local_machine=machine))


# meiva:note
# One read of the lease through the relay's credential: the lease row, and while it is
# active the pending inbox rows with their bodies.
def read_relay_inbox(
    db: Database,
    bus: EventBus,
    agent_id: int,
    lease_id: UUID,
    token: str,
    *,
    local_machine: str,
) -> InboxSnapshot:
    lease = _lease(db, bus, lease_id, token, local_machine)
    if lease.agent_id != agent_id or lease.id != lease_id:
        raise ValueError("The impersonation lease does not belong to the requested agent")
    if lease.status != ImpersonationStatus.ACTIVE:
        return _closed(lease)
    try:
        # relay_inbox validates same-machine active authority in its own transaction.
        rows = read_inbox_rows(db, str(lease_id), token, local_machine=local_machine)
    except ImpersonationError:
        latest = _lease(db, bus, lease_id, token, local_machine)
        if latest.status not in OPEN:
            return _closed(latest)
        raise
    messages = tuple(
        InboxMessage(
            id=row["id"],
            kind=row["kind"],
            source=row["source"],
            content=row["content"],
            delivery_attempts=row["delivery_attempts"],
            delivery_due=row["delivery_due"],
        )
        for row in rows
    )
    return InboxSnapshot(
        messages=messages,
        expires_at=lease.expires_at,
        status=ImpersonationStatus.ACTIVE,
        routine_ids=routine_ids(messages),
        batch_window=float(lease.relay_batch_window_seconds),
        start_message=lease.start_message,
        budget=lease.budget(),
        end_reason=None,
        ended=None,
    )


def _closed(lease: Lease) -> InboxSnapshot:
    return InboxSnapshot.without_inbox(
        lease.status,
        lease.expires_at,
        end_reason=lease.rejection_reason,
        ended=lease.terminal_notice_snapshot,
        budget=lease.budget(),
    )


# meiva:note
# The lease has ended, and the controller session is told so when the relay owns that notice.
# A lease without an immutable terminal record (a synthetic or pre-migration one) keeps the
# supplied lease scope and discloses that its historical time is unavailable.
def announce_end(snapshot: InboxSnapshot, ref: LeaseRef, adapter: RelayAdapter) -> bool:
    verdict = end_verdict(snapshot.status, notify_terminal=notifies_terminal(adapter))
    match verdict:
        case EndVerdict.CONTINUE:
            return False
        case EndVerdict.STOP_SILENT:
            return True
        case EndVerdict.STOP_WITH_NOTICE:
            deliver(adapter, notice_text(terminal_record(snapshot, ref).model_dump()))
            return True
    assert_never(verdict)


def _clock() -> ClockReading:
    wall_now = Clock(
        ClockConfig("UTC", None, message_timestamp_weekday=False), timers=SYSTEM_TIMERS
    ).now()
    return ClockReading(monotonic_now=asyncio.get_running_loop().time(), wall_now=wall_now)


# meiva:note
# The state of one relay's delivery loop: the lease it serves and the resources it drives. Its
# decisions are `relay.steps.next_step` and `plan_reservation`; this class takes the steps.
class _InboxRelay:
    def __init__(
        self,
        lease: RelayLease,
        *,
        db: Database,
        bus: EventBus,
        listener: RedisInboundListener,
        adapter: RelayAdapter,
        tuning: RelayTuning,
    ) -> None:
        self.lease = lease
        self.db = db
        self.bus = bus
        self.listener = listener
        self.adapter = adapter
        self.tuning = tuning
        self.state = RelayState.initial()

    # meiva:note
    # The current snapshot, or None once the lease ended (the end was announced).
    async def _read(self) -> InboxSnapshot | None:
        lease = self.lease
        snapshot = await asyncio.to_thread(
            read_relay_inbox,
            self.db,
            self.bus,
            lease.agent_id,
            lease.lease_id,
            lease.token,
            local_machine=lease.machine,
        )
        return None if announce_end(snapshot, lease.ref(), self.adapter) else snapshot

    # meiva:note
    # Pace, re-read, reserve and push what is due; True when the lease ended meanwhile.
    #
    # Catching an ACK or release during the pause is the point of re-reading: the pause merges
    # bursts and keeps within Claude Monitor's event allowance.
    async def _deliver(self, pause: float) -> bool:
        await asyncio.sleep(pause)
        fresh = await self._read()
        if fresh is None:
            return True
        if not fresh.active:
            return False
        lease = self.lease
        plan = plan_reservation(fresh)
        reserved: frozenset[int] = frozenset()
        if plan.ids:
            reserved = await asyncio.to_thread(
                reserve_delivery,
                self.db,
                self.bus,
                str(lease.lease_id),
                lease.token,
                list(plan.ids),
                local_machine=lease.machine,
            )
        if reserved:
            push = reserved_push(
                fresh, reserved, redelivery=plan.redelivery, ref=lease.ref(), tuning=self.tuning
            )
            deliver(self.adapter, push)
            self.state = emitted(self.state, at=_clock().monotonic_now)
        self.state = batch_closed(self.state)
        return False

    async def _loop(self) -> None:
        if await self._read() is None:
            return
        await self.listener.ensure_listening()
        while True:
            snapshot = await self._read()
            if snapshot is None:
                return
            step, self.state = next_step(
                self.state, snapshot, _clock(), self.lease.ref(), self.tuning
            )
            match step:
                case SendStart(text=text):
                    deliver(self.adapter, text)
                    self.state = emitted(self.state, at=_clock().monotonic_now)
                case WaitForWake(timeout=timeout):
                    await self.listener.wait_one(timeout)
                case Deliver(pause=pause):
                    if await self._deliver(pause):
                        return
                case _:
                    assert_never(step)

    async def run(self) -> None:
        await run_then_clean_up(self._loop(), self.listener.stop(), what="Redis listener stop")


# meiva:note
# Deliver the session start first, then respect the lease budget per pending row.
#
# Database time and durable reservations own the ACK windows and budget; local monotonic
# time only controls batching and rate limits. Every read records exhaustion across the
# whole lease, even outside this inbox page. Due retries precede fresh rows so arrivals
# cannot starve them. Reserve immediately before the host call, rechecking ACK/release races.
# A failed or ambiguous host submission spends an attempt, never an ACK.
async def push_inbox(
    lease: RelayLease,
    *,
    db: Database,
    bus: EventBus,
    listener: RedisInboundListener,
    adapter: RelayAdapter,
    tuning: RelayTuning,
) -> None:
    await _InboxRelay(
        lease, db=db, bus=bus, listener=listener, adapter=adapter, tuning=tuning
    ).run()


# meiva:note
# One durable liveness beat; False means the lease ended or the relay credential was
# revoked, and the heartbeat loop stops silently.
def write_heartbeat(db: Database, lease_id: UUID, token: str, *, local_machine: str) -> bool:
    try:
        relay_heartbeat(db, str(lease_id), token, local_machine=local_machine)
    except ImpersonationError:
        return False
    return True


async def heartbeat_loop(
    db: Database,
    lease_id: UUID,
    token: str,
    *,
    interval: float = RELAY_HEARTBEAT_SECONDS,
    local_machine: str,
) -> None:
    while True:
        await asyncio.sleep(interval)
        if not await asyncio.to_thread(
            write_heartbeat, db, lease_id, token, local_machine=local_machine
        ):
            return


# meiva:note
# Retain the original heartbeat failure until the relay stops and joins it.
async def heartbeat_completion(
    db: Database, lease_id: UUID, token: str, *, local_machine: str
) -> Exception | None:
    try:
        await heartbeat_loop(db, lease_id, token, local_machine=local_machine)
    except Exception as error:
        return error
    return None


# meiva:note
# Run `main` while `heartbeat` beats beside it, and stop the beat when `main` ends.
#
# A heartbeat failure does not cancel `main`: it surfaces after `main` stops, when the
# heartbeat is joined. Closing the task group without a body exception preserves the CLI's
# original exception type and exit-code boundary.
async def run_beside_heartbeat(
    heartbeat: Coroutine[Any, Any, Exception | None], main: Coroutine[Any, Any, None]
) -> None:
    group = asyncio.TaskGroup()
    await group.__aenter__()
    beat = group.create_task(heartbeat, name="impersonation-relay-heartbeat")
    try:
        await main
    finally:
        beat.cancel()
        try:
            with contextlib.suppress(asyncio.CancelledError):
                error = await beat
                if error is not None:
                    raise error
        finally:
            await group.__aexit__(None, None, None)


async def _serve(
    lease: RelayLease,
    *,
    db: Database,
    bus: EventBus,
    redis_url: str,
    adapter: RelayAdapter,
    tuning: RelayTuning,
) -> None:
    if not await asyncio.to_thread(
        write_heartbeat, db, lease.lease_id, lease.token, local_machine=lease.machine
    ):
        # meiva:note
        # Termination can win between startup's lease read and beat. Terminal metadata is
        # readable; ordinary inbox authority stays closed.
        ended = await asyncio.to_thread(
            read_relay_inbox,
            db,
            bus,
            lease.agent_id,
            lease.lease_id,
            lease.token,
            local_machine=lease.machine,
        )
        announce_end(ended, lease.ref(), adapter)
        return
    listener = RedisInboundListener(redis_url, lease.agent_id)
    await run_beside_heartbeat(
        heartbeat_completion(db, lease.lease_id, lease.token, local_machine=lease.machine),
        push_inbox(lease, db=db, bus=bus, listener=listener, adapter=adapter, tuning=tuning),
    )


# meiva:note
# What the handler read for one relay: the scoped credential from the channel the operator
# chose, the Redis address of the invocation's configuration, CODEX_HOME as the shell saw it,
# the Antigravity endpoint its environment named (if any), the stdout a controller session reads
# the relay's pushes from, and the invocation's random source (the bus's authentication-retry
# jitter draws from it).
@dataclass(frozen=True)
class RelayInputs:
    token: str
    redis_url: str
    codex_home: str | None
    antigravity_server: AntigravityServer | None
    stdout: TextIO
    rng: random.Random


# meiva:note
# Run a native relay until lease release/expiry, failure, or interruption. The transport
# destination binds before any lease authority is used, and the first heartbeat follows it:
# the accepting runtime reads that beat as proof the host can be pushed into, so an
# unreachable host must fail startup and roll the acceptance back. The relay never renews the
# lease; its credential reads, reserves delivery, and beats. The credential arrives as a
# value, read by the handler from the channel the operator chose.
# meiva:link docs/conventions/agents/agent-impersonation-hosts.md
def cmd_relay(
    args: argparse.Namespace,
    *,
    inputs: RelayInputs,
    database_factory: DatabaseFactory,
    machine: MachineIdentityReader,
) -> int:
    token = inputs.token
    try:
        destination = plan_destination(
            args.provider,
            args.thread_id,
            codex_remote=args.codex_remote,
            endpoint_record=args.antigravity_endpoint_file,
        )
        tuning = RelayTuning(debounce=args.debounce, max_chars=PUSH_MAX_CHARS)
        adapter = bind_adapter(
            destination,
            codex_home=inputs.codex_home,
            antigravity_server=inputs.antigravity_server,
            stdout=inputs.stdout,
        )
        local_machine = machine.name()
        db = database_factory()
        bus = EventBus.from_settings(rng=inputs.rng)
        if args.lease_id is None:
            lease_id = UUID(
                str(resolve(db, args.agent_id, args.session_id, local_machine=local_machine)["id"])
            )
        else:
            lease_id = UUID(args.lease_id)
        session_id = relay_get(db, bus, str(lease_id), token, local_machine=local_machine)[
            "session_id"
        ]
        lease = RelayLease(args.agent_id, lease_id, session_id, token, local_machine)
        asyncio.run(
            _serve(
                lease, db=db, bus=bus, redis_url=inputs.redis_url, adapter=adapter, tuning=tuning
            )
        )
    except KeyboardInterrupt:
        print(
            "Relay stopped; pending messages are unchanged and the lease is not renewed.",
            file=sys.stderr,
        )
        return 130
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"Impersonation relay stopped: {exc}", file=sys.stderr)
        return 1
    return 0
