# meiva:note
# Exec subprocess mechanics — the parent side: spawn / poll / signal / kill /
# collect one disposable child per execute_code call.
#
# The parent polls every 50ms. Every exit path ends the same way: SIGKILL the
# child's process group while its leader is still unreaped, reap the leader, drain
# the output pipe, delete the request and result files. The child leads a new
# session and process group, and holds the read end of a pipe whose write end this
# process keeps until the leader is reaped: if this process dies first, the child
# reads EOF and kills its own group. The child's result envelope stays advisory
# except for lifecycle outcomes. It spawns
# `python -I -B -X utf8 -m agent.execution.child`: isolated mode keeps the inherited cwd
# and Python environment out of bootstrap import resolution, and explicit UTF-8
# mode keeps output portable after isolated mode ignores encoding environment
# variables.

from __future__ import annotations

import asyncio
import contextlib
import signal
import subprocess
from pathlib import Path
from typing import Any
from uuid import UUID

import psutil

from agent.execution.protocol import (
    ResultPayload,
    make_request_path,
    make_result_path,
    prune_stale,
    read_result,
    write_request,
)
from base.agents.context import MeivaContext
from base.clock import SYSTEM_TIMERS
from base.deploy.release import editable_install
from base.log import logger
from base.native_process.exec_domain import KILL_GRACE_S, ExecProcessDomain
from base.native_process.exec_kill_notice import read_notice
from base.native_process.process_control import SYSTEM_PROCESS_CONTROL

from ._child_boundary import (
    ChildLaunch,
    ParentFacts,
    payload_without_envelope,
    plan_child_launch,
    result_from_payload,
)
from ._guard import inspection_refusal, violation_refusal
from ._output_pipe import ExecOutputPipe
from ._poll import Stop, plan_poll
from ._remote import run_via_exec_host
from ._result import ExecCrashed, ExecResult
from ._stream import ExecOutputChunkPublisher, StreamingTextIO

# Same cadence as the old worker-thread poll loop: near-free, and cancel /
# timeout response latency stays <= 50ms + signal delivery.
_POLL_INTERVAL_S = 0.05

# meiva:note
# A descendant that left the process group may keep the output pipe open past the
# group kill; the tail it still writes is dropped after this long.
_OUTPUT_DRAIN_TIMEOUT_S = 5.0


# meiva:note
# Inspect the interpreter source before user code; never repair it here. A system interpreter
# has no checkout authority, so there is nothing to inspect.
def _editable_guard_failure(parent: ParentFacts) -> ExecCrashed | None:
    if parent.source_root is None:
        return None
    try:
        violations = editable_install.editable_install_violations(
            parent.source_root, allowed_roots=parent.editable_roots
        )
    except Exception as exc:
        return inspection_refusal(exc)
    return violation_refusal(violations)


# meiva:note
# The child's result envelope as `(payload, error)`. A missing envelope has no payload and no
# error unless the exit code explains it (`payload_without_envelope`): the caller classifies
# from its own flags (SIGKILL, watchdog, the agent's own os._exit). A malformed envelope
# (version drift, unknown kind, oversized) is a protocol violation, so it comes back as an
# `error` string and the caller surfaces a crash outcome instead of guessing "done".
def read_result_envelope(
    result_path: Path, returncode: int | None
) -> tuple[ResultPayload | None, str | None]:
    if not result_path.exists():
        return payload_without_envelope(returncode), None
    try:
        return read_result(result_path), None
    except BaseException as exc:
        return None, f"exec result envelope could not be decoded: {exc}"


# meiva:note
# Spawn one child in its own session and process group. This process holds the
# write end of its stdin pipe until the leader is reaped; the child kills its own
# group when that end closes. Persistent ``meiva.shell.sessions`` are backend-hosted
# and outside the group.
def spawn_exec_child(launch: ChildLaunch) -> tuple[subprocess.Popen[bytes], ExecProcessDomain]:
    return ExecProcessDomain.launch_posix(
        list(launch.argv),
        process_control=SYSTEM_PROCESS_CONTROL,
        cwd=launch.cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=dict(launch.env),
    )


# meiva:note
# Whether the unreaped leader is a zombie; observing it never reaps it.
def _leader_exited(leader: psutil.Process) -> bool:
    try:
        return leader.status() in {psutil.STATUS_DEAD, psutil.STATUS_ZOMBIE}
    except psutil.NoSuchProcess:
        return True


# meiva:note
# Poll every 50ms and publish chunks. Signals SIGINT to the group on cancel and
# SIGTERM on deadline. Returns (cancelled, timed_out); on a same-tick race cancel wins.
# What each beat does is `plan_poll`'s decision.
async def _poll_child(
    domain: ExecProcessDomain,
    leader: psutil.Process,
    stream: StreamingTextIO,
    chunk_publisher: ExecOutputChunkPublisher | None,
    cancel_event: asyncio.Event,
    timeout: float,
    reader: ExecOutputPipe,
) -> tuple[bool, bool]:
    started = SYSTEM_TIMERS.monotonic()
    deadline = started + timeout
    last_activity = started
    while not _leader_exited(leader):
        reader.pump()
        pending = stream.take_pending() if chunk_publisher is not None else ""
        now = SYSTEM_TIMERS.monotonic()
        plan = plan_poll(
            pending=bool(pending),
            now=now,
            last_activity=last_activity,
            deadline=deadline,
            cancel_requested=cancel_event.is_set(),
        )
        if chunk_publisher is not None and plan.forward:
            chunk_publisher.publish(pending)
            last_activity = now
        elif chunk_publisher is not None and plan.keepalive:
            chunk_publisher.keepalive()
            last_activity = now
        if plan.stop is Stop.CANCEL:
            domain.signal(signal.SIGINT)
            return True, False
        if plan.stop is Stop.DEADLINE:
            domain.signal(signal.SIGTERM)
            return False, True
        await asyncio.sleep(_POLL_INTERVAL_S)
    return False, False


# meiva:note
# Give a signalled leader its grace window to exit on its own.
async def _await_leader_exit(domain: ExecProcessDomain, leader: psutil.Process) -> None:
    deadline = SYSTEM_TIMERS.monotonic() + KILL_GRACE_S
    while not _leader_exited(leader):
        if SYSTEM_TIMERS.monotonic() >= deadline:
            logger.warning(
                "[{label}] exec child {pid} survived the {grace}s grace period — "
                "hard-stopping its process group (native-stuck code or swallowed signal)",
                label="exec-subprocess-killed",
                pid=domain.proc.pid,
                grace=KILL_GRACE_S,
                event="exec_subprocess_killed",
            )
            return
        await asyncio.sleep(_POLL_INTERVAL_S)


# meiva:note
# SIGKILL the group while the leader is unreaped, reap it, then release the control pipe.
#
# The write end closes only after the reap: a child that reads EOF kills its group.
def _close_domain(domain: ExecProcessDomain) -> None:
    try:
        domain.kill_and_reap()
    finally:
        if domain.proc.stdin is not None:
            domain.proc.stdin.close()


def _close_output(reader: ExecOutputPipe) -> None:
    if not reader.closed:
        logger.warning(
            "exec output pipe stayed open after its process group was killed; "
            "a descendant outside the group holds it, so the rest of its output is dropped"
        )
        reader.close()


def _write_request_failure(
    path: Path, code: str, context: MeivaContext, timeout: float, state: dict[str, Any] | None
) -> ExecCrashed | None:
    try:
        write_request(
            path,
            code=code,
            context=context.describe(),
            timeout_s=timeout,
            state=state,
            incarnation=context.original_incarnation,
        )
    except BaseException as exc:
        return ExecCrashed(output=f"exec subprocess request could not be written: {exc}", exc=exc)
    return None


async def _run_local_child(
    *,
    parent: ParentFacts,
    agent_id: int | None,
    request_path: Path,
    result_path: Path,
    timeout: float,
    stream: StreamingTextIO,
    chunk_publisher: ExecOutputChunkPublisher | None,
    cancel_event: asyncio.Event,
    config_overlay: dict[str, object] | None,
    birth_config: dict[str, object] | None,
) -> tuple[int | None, bool, bool] | ExecCrashed:
    domain: ExecProcessDomain | None = None
    reader: ExecOutputPipe | None = None
    try:
        try:
            launch = plan_child_launch(
                parent,
                agent_id,
                request_path,
                result_path,
                config_overlay=config_overlay,
                birth_config=birth_config,
            )
            proc, domain = spawn_exec_child(launch)
        except OSError as exc:
            return ExecCrashed(
                output=f"exec subprocess could not be spawned: {exc}",
                exc=exc,
            )

        leader = psutil.Process(proc.pid)
        reader = ExecOutputPipe(proc, stream)
        reader.watch()

        cancelled, timed_out = await _poll_child(
            domain, leader, stream, chunk_publisher, cancel_event, timeout, reader
        )
        if cancelled or timed_out:
            await _await_leader_exit(domain, leader)
        _close_domain(domain)
        await reader.finish(_OUTPUT_DRAIN_TIMEOUT_S)
        _close_output(reader)

        if chunk_publisher is not None:
            pending = stream.take_pending()
            if pending:
                chunk_publisher.publish(pending)

        return proc.returncode, cancelled, timed_out
    except BaseException as original:
        # meiva:note
        # Cancellation by the exec node's outer shield (asyncio.wait_for) and any other
        # failure leave nothing running: the same synchronous, bounded close as above.
        if domain is not None:
            try:
                _close_domain(domain)
                if reader is not None:
                    reader.finish_now(_OUTPUT_DRAIN_TIMEOUT_S)
                    _close_output(reader)
            except Exception as cleanup:
                original.add_note(f"exec cleanup failed: {cleanup!r}")
        raise


# meiva:note
# Run `code` in one disposable child process; poll every 50ms monitoring
# exit / cancel_event / deadline (same priority as the old thread loop:
# cancel > deadline > natural completion). When `exec-host.sock` is listening
# in `exec_dir.parent`, delegates child spawn and process-group supervision to
# `exec-host`; otherwise spawns `agent.execution.child` locally.
#
# Returns the `ExecResult` sum type the exec node dispatches plus the raw
# child envelope (None when the child never wrote one — SIGKILL / watchdog /
# os._exit): the envelope carries the state-update delta (plugin fields,
# security findings), which only this function can see.
# The child's outcome kinds map onto the result with the parent's flags
# authoritative.
async def run_in_subprocess(
    code: str,
    context: MeivaContext,
    cancel_event: asyncio.Event,
    timeout: float,
    chunk_publisher: ExecOutputChunkPublisher | None = None,
    *,
    accumulation_max_chars: int,
    parent: ParentFacts,
    exec_dir: Path,
    request_id: UUID,
    result_id: UUID,
    state: dict[str, Any] | None = None,
    config_overlay: dict[str, object] | None = None,
    birth_config: dict[str, object] | None = None,
) -> tuple[ExecResult, ResultPayload | None]:
    guard_failure = _editable_guard_failure(parent)
    if guard_failure is not None:
        return guard_failure, None
    agent_id = context.require_identity().agent_id
    prune_stale(exec_dir, agent_id, now=context.require_clock().now())
    request_path = make_request_path(exec_dir, agent_id, request_id=request_id)
    result_path = make_result_path(exec_dir, agent_id, result_id=result_id)

    request_error = _write_request_failure(request_path, code, context, timeout, state)
    if request_error is not None:
        return request_error, None

    stream = StreamingTextIO(max_chars=accumulation_max_chars)
    try:
        outcome = await run_via_exec_host(
            exec_dir=exec_dir,
            parent=parent,
            agent_id=agent_id,
            request_path=request_path,
            result_path=result_path,
            timeout=timeout,
            stream=stream,
            chunk_publisher=chunk_publisher,
            cancel_event=cancel_event,
            config_overlay=config_overlay,
            birth_config=birth_config,
        )
        if outcome is None:
            outcome = await _run_local_child(
                parent=parent,
                agent_id=agent_id,
                request_path=request_path,
                result_path=result_path,
                timeout=timeout,
                stream=stream,
                chunk_publisher=chunk_publisher,
                cancel_event=cancel_event,
                config_overlay=config_overlay,
                birth_config=birth_config,
            )
        if isinstance(outcome, ExecCrashed):
            return outcome, None
        returncode, cancelled, timed_out = outcome
        payload, envelope_error = read_result_envelope(result_path, returncode)
        return (
            result_from_payload(
                stream.getvalue(),
                payload,
                cancelled=cancelled,
                timed_out=timed_out,
                envelope_error=envelope_error,
                stream_cap=stream.cap(),
                memory_guard_notice=read_notice(result_path),
            ),
            payload,
        )
    finally:
        for path in (request_path, result_path):
            with contextlib.suppress(FileNotFoundError):
                path.unlink()


__all__ = ["run_in_subprocess"]
