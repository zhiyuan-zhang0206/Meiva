"""The actual resources retained by one hosted turn's composition root."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from typing import Any

from base.log import logger
from base.native_process.stop_timing import CANCEL_UNWIND_TIMEOUT_S


# meiva:note
# The identity of one hosted turn Task within its service; never held by the model cache.
@dataclass
class HostedTurnResources:
    service: HostedServiceResources | None = None

    def require_service(self) -> HostedServiceResources:
        """Late work needs the actual service lifespan, beyond this turn's return."""
        if self.service is None:
            raise RuntimeError("hosted asynchronous resources require their service owner")
        return self.service


class HostedServiceResources:
    """The host's cross-turn completion span, joined before its database pools close.

    A late failure is retained and reported immediately without cancelling other
    turns. Only service stop/join raises it. Its original resource scope stays
    alive with the failure; observing an error never certifies resource cleanup.
    """

    def __init__(self) -> None:
        self._tasks = asyncio.TaskGroup()
        self._started = False
        self._closed = False
        self._joined = False
        self._pending: set[asyncio.Task[Any]] = set()
        self._turns: set[asyncio.Task[Any]] = set()
        self.failures: list[tuple[HostedTurnResources, BaseException]] = []

    async def turn(self) -> HostedTurnResources:
        if self._closed:
            raise RuntimeError("hosted resource service is already closed")
        if not self._started:
            await self._tasks.__aenter__()
            self._started = True
        return HostedTurnResources(service=self)

    @contextmanager
    def hold_turn(self, task: asyncio.Task[Any] | None) -> Generator[None]:
        """Pool closure also waits for the actual roots that can create more resource work."""
        if task is None:
            raise RuntimeError("a hosted turn requires its actual task owner")
        self._turns.add(task)
        try:
            yield
        finally:
            self._turns.discard(task)

    def watch(self, operation: Coroutine[Any, Any, None], *, name: str) -> asyncio.Task[None]:
        """Start a subscription whose invocation consumes its observed result."""
        task = self._tasks.create_task(operation, name=name)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        return task

    def retain_task(self, scope: HostedTurnResources, task: asyncio.Task[Any]) -> None:
        """Retain an original turn child without changing its result or cancelling siblings.

        Existing turns may create settlement work after service stop begins.
        The same pending set keeps that actual Task inside the original join
        deadline, even if its caller has already returned or raised.
        """
        if scope.service is not self:
            raise RuntimeError("a hosted task requires its original service scope")
        if self._closed and self.joined:
            raise RuntimeError("hosted resource service has already joined")
        self._pending.add(task)
        task.add_done_callback(partial(self._collect_task, scope))

    def _collect_task(self, scope: HostedTurnResources, task: asyncio.Task[Any]) -> None:
        self._pending.discard(task)
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                self.record_failure(scope, error, name=task.get_name())

    def record_failure(
        self, scope: HostedTurnResources, error: BaseException, *, name: str
    ) -> None:
        if any(prior is scope and previous is error for prior, previous in self.failures):
            return
        self.failures.append((scope, error))
        logger.opt(exception=error).error(
            "late hosted resource failure retained until service stop/join: {name}", name=name
        )

    @property
    def joined(self) -> bool:
        return self._joined or (not self._started and not self._turns and not self._pending)

    async def _wait_until_deadline(
        self, deadline: float
    ) -> tuple[set[asyncio.Task[Any]], asyncio.CancelledError | None]:
        loop = asyncio.get_running_loop()
        cancellation: asyncio.CancelledError | None = None
        pending: set[asyncio.Task[Any]] = {
            task for task in self._pending | self._turns if not task.done()
        }
        while pending and loop.time() < deadline:
            try:
                await asyncio.wait(pending, timeout=max(0, deadline - loop.time()))
                pending = {task for task in self._pending | self._turns if not task.done()}
            except asyncio.CancelledError as error:
                cancellation = error
                pending = {task for task in self._pending | self._turns if not task.done()}
        return pending, cancellation

    async def aclose(self, *, deadline: float | None = None) -> None:
        """Bound actual join by the host's remaining cancellation-diagnostic budget.

        On expiry the group and exact tasks stay owned. The daemon must leave
        their clients/pools open for its existing hard-exit boundary; a timed
        wait cannot make an unfinished poll or domain close safe to release.
        """
        self._closed = True
        if deadline is None:
            deadline = asyncio.get_running_loop().time() + CANCEL_UNWIND_TIMEOUT_S
        pending, cancellation = await self._wait_until_deadline(deadline)
        errors = [error for _scope, error in self.failures]
        if cancellation is not None:
            errors.insert(0, cancellation)
        if pending:
            error = TimeoutError(
                "hosted resource service join remains unfinished: "
                + ", ".join(sorted(task.get_name() for task in pending))
            )
            logger.opt(exception=error).error(
                "hosted resource service exhausted its join budget; "
                "original tasks and clients remain owned until host hard exit"
            )
            errors.append(error)
        elif self._started and not self._joined:
            await self._tasks.__aexit__(None, None, None)
            self._joined = True
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("late hosted resource failures", errors)
