# meiva:link gateway/upload_delivery/docs/delivered-uploads/delivered-uploads.meiva.okf.md

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from functools import partial
from pathlib import Path
from typing import Any

from psycopg_pool import ConnectionPool
from pydantic import ValidationError

from base import telemetry
from base.agents.impersonation.wake import publish_inbound_wake
from base.agents.messages.inbound import InboundKind
from base.agents.upload_delivery import source, staging
from base.agents.upload_delivery.models import (
    CopyProof,
    ReceiveRequest,
    UploadDeliveryConflictError,
)
from base.agents.upload_delivery.paths import STAGED_FILE_TTL_SECONDS
from base.api_contracts.agent_contract import AgentNotFound
from base.cluster.machine import MachineIdentityReader
from base.db import Database
from base.events.live.bus import EventBus
from gateway.http.middleware.stopping import is_stopping
from gateway.upload_delivery.failure_plan import CopyFailure, receiver_failure, terminal_failure
from ops.cluster.rpc import ClusterOpFailed, ClusterOpTargetAbsent, RpcClient
from ops.lifecycle import resurrect_if_terminated

_log = logging.getLogger(__name__)


# meiva:note
# The pool is read per round, not kept: `app.state.db_pool` is a runtime fact that a restart or a
# reconnect may replace, and a worker holding the object it was built with keeps borrowing from the
# retired one -- silently, because that pool stays open until the process closes it.
PoolProvider = Callable[[], ConnectionPool]


class UploadRecovery:
    """A source domain round and its actual native futures; never time-based leases."""

    def __init__(
        self,
        pool_provider: PoolProvider,
        db: Database,
        bus: EventBus,
        *,
        machine: MachineIdentityReader,
        home: Path,
        claim_seconds: float,
        rpc: RpcClient,
    ) -> None:
        self.pool_provider, self.db, self.bus = pool_provider, db, bus
        self.rpc = rpc
        self.machine = machine
        self.home = home
        self.claim_seconds = claim_seconds
        self.stopped = asyncio.Event()
        self.nudged = asyncio.Event()
        self.calls: set[asyncio.Future[Any]] = set()
        self.task: asyncio.Task[None] | None = None

    # meiva:note
    # Every use re-reads the provider: one round may span a swap, and a method that kept the
    # object it started with would write one half of the round into the retired pool.
    @property
    def pool(self) -> ConnectionPool:
        return self.pool_provider()

    def nudge(self) -> None:
        self.nudged.set()

    async def native[T](self, call: Callable[..., T], *args: object, **kwargs: object) -> T:
        task = asyncio.get_running_loop().run_in_executor(None, partial(call, *args, **kwargs))
        self.calls.add(task)
        task.add_done_callback(self.finished_call)
        # Cancellation does not stop a DB/filesystem thread. Retain it until the
        # actual call finishes, and lifespan.close drains it before pool closure.
        return await asyncio.shield(task)

    def finished_call(self, future: asyncio.Future[Any]) -> None:
        self.calls.discard(future)
        if not future.cancelled():
            future.exception()  # Consume errors even if the HTTP await was cancelled.

    def start(self, group: asyncio.TaskGroup) -> None:
        self.task = group.create_task(self.run())

    async def close(self) -> None:
        self.stopped.set()
        self.nudged.set()
        cancelled = False
        if self.task is not None:
            while not self.task.done():
                try:
                    await asyncio.shield(self.task)
                except asyncio.CancelledError:
                    cancelled = True
        while self.calls:
            try:
                await asyncio.shield(asyncio.gather(*self.calls, return_exceptions=True))
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError

    async def run(self) -> None:
        while not self.stopped.is_set():
            self.nudged.clear()
            try:
                if not is_stopping():
                    await self.round()
            except Exception:
                _log.exception(
                    "delivered upload recovery round failed; retained intents remain recoverable"
                )
            if self.stopped.is_set():
                break
            with suppress(TimeoutError):
                await asyncio.wait_for(self.nudged.wait(), timeout=5.0)

    async def round(self) -> None:
        machine_name = self.machine.name()
        pool = self.pool_provider()
        requests = await self.native(
            source.due,
            pool,
            4,
            local_machine=machine_name,
            home=self.home,
            claim_seconds=self.claim_seconds,
        )
        async with asyncio.TaskGroup() as group:
            for request in requests:
                group.create_task(self.copy(request))
        if not self.stopped.is_set():
            await self.native(
                staging.release_accepted,
                pool,
                local_machine=machine_name,
                home=self.home,
            )
            await self.native(
                staging.sweep_unsent,
                pool,
                local_machine=machine_name,
                home=self.home,
                ttl_seconds=STAGED_FILE_TTL_SECONDS,
            )

    async def wake(self, agent_id: int, inbound_id: int) -> None:
        """Wake the target once after acceptance; a failure is logged and not retried.

        The accepted chat is already a pending inbound, so the agent's next ordinary wake
        (a message or heartbeat) delivers it.
        """
        try:
            await self.native(publish_inbound_wake, self.db, self.bus, agent_id, str(inbound_id))
            await resurrect_if_terminated(
                self.db,
                self.bus,
                agent_id,
                trigger_inbound_id=inbound_id,
                trigger_inbound_kind=InboundKind.CHAT,
                local_machine=self.machine.name(),
                rpc=self.rpc,
            )
        except Exception:
            _log.warning(
                "upload inbound wake failed for agent=%s iid=%s; not retried",
                agent_id,
                inbound_id,
                exc_info=True,
            )

    async def copy(self, request: ReceiveRequest) -> None:
        if self.stopped.is_set():
            return
        try:
            if not await self.native(source.validate_pending, self.pool, request):
                return
            proof = await self.copy_proof(request)
            # meiva:note
            # A stopping gateway finishes its owned copy, but does not admit a fresh chat.
            # Retained pending intent retries after the next start.
            if self.stopped.is_set():
                return
            inbound_id = await self.native(source.complete, self.pool, request, proof)
        except ClusterOpFailed as exc:
            await self.failure(request, receiver_failure(exc.result))
        except (
            UploadDeliveryConflictError,
            AgentNotFound,
            ValidationError,
            ClusterOpTargetAbsent,
        ) as exc:
            conflict = isinstance(exc, UploadDeliveryConflictError)
            await self.failure(
                request, terminal_failure(type(exc).__name__, str(exc), conflict=conflict)
            )
        except Exception as exc:
            await self.failure(request, CopyFailure(type(exc).__name__, hold=False))
        else:
            try:
                await self.native(
                    staging.release_accepted,
                    self.pool,
                    local_machine=self.machine.name(),
                    home=self.home,
                    batch_id=request.manifest.batch_id,
                )
            except Exception:
                _log.warning(
                    "staged upload release failed for batch=%s; will retry on next round",
                    request.manifest.batch_id,
                    exc_info=True,
                )
            await self.wake(request.manifest.agent_id, inbound_id)

    # meiva:note
    # Every delivery is the receiver's copy, including one that stays on this machine: the
    # staging directory an accepted batch lives in is not the agent's workspace, so "same unit"
    # no longer means "already in place". The receiver reserves quota against the workspace
    # directory and publishes create-only, so a local delivery is accounted for exactly like a
    # remote one.
    async def copy_proof(self, request: ReceiveRequest) -> CopyProof:
        result = await self.rpc.dispatch_to_machine(
            self.db,
            request.target.machine,
            "upload-receive-v2",
            request.model_dump(mode="json"),
            timeout_s=130.0,
            retries=0,
        )
        return CopyProof.model_validate(result)

    async def failure(self, request: ReceiveRequest, failure: CopyFailure) -> None:
        state = await self.native(
            lambda: source.record_failure(
                self.pool, request.manifest.batch_id, failure.reason, hold=failure.hold
            )
        )
        if state == "hold":
            telemetry.emit(
                "telemetry",
                "upload_delivery_held",
                attributes={
                    "agent_id": request.manifest.agent_id,
                    "batch_id": request.manifest.batch_id,
                    "reason": failure.reason,
                },
            )
