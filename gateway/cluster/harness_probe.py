from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable

from fastapi import HTTPException

from base.cluster import machines
from base.cluster.machine import MachineIdentityReader
from base.db import Database
from gateway.cluster.ops_dispatch import dispatch_op
from gateway.cluster.schemas import HarnessAvailability
from ops.cluster.rpc import RpcClient
from ops.rpc_schemas import HarnessProbeResult


# meiva:note
# Reads retain the last explicit probe for this gateway process. Reading or
# expiring a snapshot never starts a machine probe; only refresh does that.
# meiva:link base/agents/docs/harness/availability.meiva.okf.md
class HarnessProbeCache:
    def __init__(self) -> None:
        self._rows: dict[str, list[HarnessAvailability]] = {}
        self._lock = threading.Lock()
        self._refreshes: dict[str, asyncio.Future[list[HarnessAvailability]]] = {}

    def get(self, machine: str) -> list[HarnessAvailability] | None:
        with self._lock:
            return self._rows.get(machine)

    def store(self, machine: str, rows: list[HarnessAvailability]) -> None:
        with self._lock:
            self._rows[machine] = rows

    async def refresh(
        self, machine: str, probe: Callable[[], Awaitable[list[HarnessAvailability]]]
    ) -> list[HarnessAvailability]:
        pending = self._refreshes.get(machine)
        if pending is not None:
            return await asyncio.shield(pending)
        pending = asyncio.get_running_loop().create_future()
        self._refreshes[machine] = pending
        # meiva:note
        # The first HTTP request owns the probe directly; followers share only
        # its result. Cancelling a follower cannot cancel that request's work.
        # Cancelling the leader ends its probe and all followers honestly.
        # meiva:link base/agents/docs/harness/availability.meiva.okf.md
        try:
            rows = await probe()
            self.store(machine, rows)
            pending.set_result(rows)
            return rows
        except BaseException as exc:
            pending.set_exception(exc)
            pending.exception()
            raise
        finally:
            self._refreshes.pop(machine, None)


# meiva:note
# Only a machine that can run agents answers: a box without an agent-runner cannot
# birth one there, so its install state would be an answer to a question nobody can
# act on — it reports 503 like any other unrunnable target, and the form shows
# `unknown` rather than a `launchable` option that leads nowhere.
# meiva:link base/agents/docs/harness/availability.meiva.okf.md
async def require_harness_target(
    target: str, *, db: Database, local_machine: MachineIdentityReader
) -> None:
    if target == local_machine.name():
        role = local_machine.role()
    else:
        try:
            role = await asyncio.to_thread(machines.lookup_role, db, target)
        except machines.MachineNotRegistered:
            raise HTTPException(status_code=404, detail=f"unknown machine: {target!r}") from None
    if "agent-runner" not in role:
        raise HTTPException(
            status_code=503,
            detail=(
                f"machine {target!r} has no agent-runner ops server — "
                "it cannot run an agent, so it cannot report which harnesses it can run"
            ),
        )


async def probe_harnesses(
    target: str, *, db: Database, local_machine: MachineIdentityReader, rpc: RpcClient
) -> HarnessProbeResult:
    await require_harness_target(target, db=db, local_machine=local_machine)
    return HarnessProbeResult.model_validate(
        await dispatch_op(db, target, "harness_probe", {}, rpc=rpc)
    )
