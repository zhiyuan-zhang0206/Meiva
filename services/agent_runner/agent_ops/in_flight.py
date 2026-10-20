# meiva:note
# The requests an ops server has admitted and the executor work they started.
#
# A request is counted, as its handler task, from admission until the handler returns; a worker
# from submission until its thread finishes: cancelling the await of either does not end the
# work, so both stay visible until it really does. SIGTERM drains them within a bound
# (`serve_until_sigterm`).

import asyncio
import logging
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from base.daemon.shutdown import sigterm_requests_exit
from base.native_process.stop_timing import OPS_DRAIN_S

RequestTokens = set[asyncio.Task[Any]]
WorkerFutures = set[asyncio.Future[Any]]

_log = logging.getLogger("services.agent_runner.agent_ops.in_flight")
_DRAIN_POLL_S = 0.1


# meiva:note
# Count one request, by its handler task, until its work finishes.
@contextmanager
def admitted(*, requests: RequestTokens) -> Generator[None]:
    handler = asyncio.current_task()
    if handler is None:
        raise RuntimeError("a request is admitted from the task that handles it")
    requests.add(handler)
    try:
        yield
    finally:
        requests.remove(handler)


def track_worker(future: asyncio.Future[Any], *, workers: WorkerFutures) -> None:
    workers.add(future)
    future.add_done_callback(workers.discard)


# meiva:note
# Wait for the admitted requests and their executor work to finish, for at most `budget_s`.
#
#     What is still running when the budget is spent is named at ERROR. The handlers of the
#     requests left are cancelled, so their connections close and the server's own close
#     does not wait on them; the executor threads cannot be cancelled and die with the
#     process. A request that never finished has no result to deliver: its caller sees the
#     connection close and the operation's own idempotency key decides what a retry does.
async def drain(
    *, requests: RequestTokens, workers: WorkerFutures, budget_s: float = OPS_DRAIN_S
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_s
    while (requests or workers) and loop.time() < deadline:
        await asyncio.sleep(_DRAIN_POLL_S)
    if requests or workers:
        _log.error(
            "[ops] stop drain budget of %.0fs spent with %d request(s) and %d worker(s) "
            "still running; cancelling them",
            budget_s,
            len(requests),
            len(workers),
        )
        for handler in requests:
            handler.cancel()


# meiva:note
# Serve until SIGTERM, then stop taking connections and let what was admitted finish.
#
#     `sigterm_requests_exit` takes SIGTERM over for the block, so the exit runs inside the
#     loop as a bounded drain instead of cancelling every in-flight request at once: SIGTERM
#     cancels this task out of `serve_forever`, and only that cancellation is absorbed. A
#     server that stops serving by itself is a failure, not a stop, and propagates.
# meiva:link services/agent_runner/agent_host/lifecycle/docs/graceful-exit.meiva.okf.md
async def serve_until_sigterm(
    server: asyncio.Server, *, requests: RequestTokens, workers: WorkerFutures
) -> None:
    serving = asyncio.current_task()
    if serving is None:
        raise RuntimeError("the ops server is served from a task")
    requested = False

    def request() -> None:
        nonlocal requested
        requested = True
        serving.cancel()

    with sigterm_requests_exit("ops", request):
        try:
            await server.serve_forever()
        except asyncio.CancelledError:
            if not requested or serving.uncancel() > 0:
                raise
    server.close()
    await drain(requests=requests, workers=workers)
