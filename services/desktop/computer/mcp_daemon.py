"""Per-machine shared computer-use MCP service.

One daemon executes every desktop action on this machine, between agents and the desktop:

- Every action goes through the signed permissions helper
  (`base.host.permissions_helper.client`), the only process holding the macOS
  TCC screen-recording / accessibility grants — no new code path ever touches
  CGEvent / screencapture directly.
- Actions are serialized machine-wide with one asyncio lock around execute.
  A multi-step snapshot never interleaves with another agent's click, matching
  the browser-mcp daemon's serialization.
- Every action is audited as a `computer_action` event (outcome ok / error)
  — facts for later review, nothing is refused here. Per-agent permission
  division is a prompt-level convention between peers (user ruling 2026-08-10),
  not code-enforced governance; the cluster's security boundary is its entry
  point, not this daemon.
- Phase 2: screen ownership is coordinated (holder + renewable lease + FIFO
  queue + release_control + operator kick — `services/desktop/computer/session.py`);
  `snapshot(include_ocr=true)` adds Vision OCR text boxes
  (`services/desktop/computer/ocr/__init__.py`); task_id calls get a computer_session_start/end
  envelope (`services/desktop/computer/task_sessions.py`).
- OCR text tools (task #2401): `find_text` locates recognized text and
  returns its physical-pixel boxes; `click_text` OCRs, locates, and clicks in
  one serialized action — the same Vision OCR `snapshot(include_ocr=true)`
  exposes, wrapped so callers act on what they read on screen.

Module map: `screen.py` owns capture and coordinate translation;
`ocr_text.py` owns OCR and text tools; `execute.py` owns the MCP declarations
and per-tool execution; `errors.py` owns the shared tool error. This module
keeps the daemon lifecycle, serialization, screen ownership, and auditing.

Wire types and request/response examples live in `protocol.py`. The per-agent
bridge (`services/desktop/computer/mcp_wrapper.py`) and the MCP daemon's direct
dial (`meiva/mcps/_computer.py`) speak that protocol; `agent_id` is stamped by the
bridge from the calling agent's identity and rides into the audit stream, where
it is likewise self-reported by the agent's own process.

Coordinate contract: every tool coordinate is in PHYSICAL pixels — the same
space as the `snapshot` PNG, so a caller can click exactly where it saw a
pixel. The daemon divides by the backing scale before calling the helper,
whose CGEvent space is logical points (Retina 2x on macOS). The scale is
measured from the capture (PNG physical size vs logical screen size), not taken from the helper
report — the helper holds no AppKit event loop and can serve a stale scale
(2026-08-30: scale 2 on a 1x display, halving every click). `snapshot` returns
the physical pixel size and the logical screen size + the measured scale.

Run as a supervised daemon (ServiceSpec session "computer-mcp"):
    .venv/bin/python -m services.desktop.computer.mcp_daemon
"""

from __future__ import annotations

import asyncio
import faulthandler
import json
import os
import random
import signal
import sys
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from pydantic import TypeAdapter

from base.clock import SYSTEM_TIMERS, Clock, Timers
from base.clock.configuration import clock_config_from_boot
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine import GatewayAccess
from base.cluster.machine.boot import gateway_access_from_boot
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot, settings
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.host.boundary.fields import optional_int_field
from base.host.boundary.json_values import is_object_dict
from base.host.exit_codes import hard_exit
from base.host.permissions_helper.client import PermissionsHelperError
from base.host.remote_hosts import (
    RemoteHostConfig,
    RemoteHostConfigError,
    ResolvedTarget,
    TargetResolutionError,
)
from base.host.system.hostport import Host
from base.host.system.probes import permissions_helper_incapability
from base.log import logger
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.paths import computer_mcp_socket, meiva_home
from base.sessions.helperproc import helper_client
from base.telemetry import build_pipeline
from base.telemetry.process_logging import init_gateway_process
from services.desktop.remote import ClusterToolCaller, RemoteTransportRouter

from . import session
from .config import (
    ComputerUseConfig,
    DaemonSettingsLike,
    beat_interval_s,
    effective_stall_s,
    from_daemon_settings,
    loop_is_silent,
)
from .execute import TOOLS, DesktopHelper, mcp_result, priority
from .protocol import Response, WireRequest
from .screen import snapshot_stamp
from .task_sessions import TaskSessionTracker

# A snapshot PNG can be multi-MB on one line; lift the stream buffer cap well
# above StreamReader's 64KiB default (same limit as the browser daemon).
LINE_LIMIT = 64 * 1024 * 1024


def _hide_badge(helper: DesktopHelper) -> None:
    """End the capture badge when the screen is handed over. The helper shows it, per input, on
    the window the input lands on, and ends it on its own; this only makes it prompt. It is
    display-only, so a failure is logged and never fails the release."""
    try:
        helper.indicator_stop()
    except (PermissionsHelperError, OSError) as e:
        logger.warning(f"[computer-mcp] capture indicator not stopped, its lease will expire: {e}")


def computer_use_config(daemon_settings: DaemonSettingsLike | None = None) -> ComputerUseConfig:
    """The composition root: the one place this package reads `settings`."""
    return from_daemon_settings(settings.daemon if daemon_settings is None else daemon_settings)


# ── loop-liveness watchdog ──────────────────────────────────────────────────
# Tools execute synchronously on the event loop (one action at a time
# machine-wide). A sync call that never returns therefore wedges the whole
# daemon: it stops accepting connections, cannot answer the healthcheck's
# ping, and cannot even run its SIGTERM handler (Python only reaches that
# handler through the loop). The 2026-10-04 incident: a wedged loop left the
# unit unstoppable for ~15 minutes until a manual kill, because a blocked
# loop cannot exit for the supervisor to respawn it, and the root stop
# window (10s) passed five times into the restart breaker. The watchdog
# bounds the blocked-loop half of that failure class: the loop re-beats a
# timestamp on every tick and a background thread checks it; when the loop
# has not beaten for the configured window, the watchdog dumps every
# thread's stack to stderr (captured into the unit's output.log by the
# supervisor) and exits(1) — a dead process the supervisor restarts on its
# normal path. The incident's other half, a shutdown that never finished, is
# bounded by `bounded_cleanup` below. Either serving, or gone.
_WATCHDOG_BEAT_S = 15.0  # loop-side re-beat interval (watchdog sampling detail)
_WATCHDOG_CHECK_S = 5.0  # watchdog-thread check interval (watchdog sampling detail)


# meiva:note
# Dump every stack and exit once the event loop has not beaten for `stall_s`.
#
# The only beat source is the loop's own tick callback, so a blocked loop stops beating
# while a live loop keeps the watchdog silent no matter how long a *caller* waits. The exit
# is `dump_and_exit`, which ends the process, so a stall never returns to the caller.
class LoopWatchdog(threading.Thread):
    def __init__(self, stall_s: float, *, check_s: float, timers: Timers) -> None:
        super().__init__(name="computer-mcp-loop-watchdog", daemon=True)
        self._stall_s = stall_s
        self._check_s = check_s
        self._timers = timers
        self._beat = timers.monotonic()
        self._stopped = threading.Event()

    def beat(self) -> None:
        """Prove the loop is alive (called from the loop's tick callback)."""
        self._beat = self._timers.monotonic()

    def stale_for(self) -> float:
        """Seconds since the last beat."""
        return self._timers.monotonic() - self._beat

    @property
    def stopped(self) -> bool:
        return self._stopped.is_set()

    def stop(self) -> None:
        self._stopped.set()

    def run(self) -> None:
        while not self._stopped.wait(self._check_s):
            if loop_is_silent(
                now=self._timers.monotonic(), last_beat=self._beat, stall_s=self._stall_s
            ):
                dump_and_exit(f"run loop silent for over {self._stall_s}s")
                return


def dump_and_exit(reason: str) -> None:
    """The last resort of every bound in this module: dump every thread's
    stack, then exit for a supervisor restart.

    Anything that reaches here passed its configured bound while the unit was
    still not serving; the only exit left that the supervisor can act on is a
    dead process. Raw stderr writes, not logging: the log path may be what is
    blocked. The supervisor captures stderr into the unit's output.log, so
    the dump — the forensics the 2026-10-04 wedge lacked — survives the exit.
    `reason` names the bound that tripped, so the first line of the dump
    already says which one it was.
    """
    try:
        sys.stderr.write(
            f"[computer-mcp] {reason} — thread stacks follow; exiting for a supervisor restart\n"
        )
        sys.stderr.flush()
        faulthandler.dump_traceback(all_threads=True)
    finally:
        os._exit(1)


# meiva:note
# Start the loop-liveness watchdog and keep it beating from `loop`.
#
# Returns the watchdog so the shutdown path can stop it. The re-beat cadence stays well
# inside the window (`beat_s` shrunk to a third of it when the configured window is small),
# so an operator tuning the window down cannot trip a healthy loop; a tiny window is floored
# so a misconfiguration cannot restart-loop the daemon.
def guard_loop_liveness(
    loop: asyncio.AbstractEventLoop,
    stall_s: float,
    *,
    timers: Timers,
    beat_s: float = _WATCHDOG_BEAT_S,
    check_s: float = _WATCHDOG_CHECK_S,
) -> LoopWatchdog:
    stall_s = effective_stall_s(stall_s)
    watchdog = LoopWatchdog(stall_s, check_s=check_s, timers=timers)
    watchdog.start()
    beat_interval = beat_interval_s(beat_s, stall_s)

    def tick() -> None:
        if watchdog.stopped:
            return
        watchdog.beat()
        loop.call_later(beat_interval, tick)

    tick()
    return watchdog


async def bounded_cleanup(
    server: asyncio.AbstractServer, clients: set[asyncio.Task[None]], *, drain_s: float
) -> None:
    """Close out the clients and the listener, bounded by `drain_s`.

    `server.close()` has already closed the listening socket; `wait_closed()`
    returns only once every handler task is done, and a client holding its
    connection open (the SDK keeps one persistent socket) or a handler stuck
    in its own unwind would otherwise hang shutdown forever — the second half
    of the 2026-10-04 wedge: listener closed (every later connect refused,
    `Errno 61`), the stop signal already consumed, and the process neither
    serving nor dying until a manual kill.

    The clients wait is `asyncio.wait`, not `gather`, and deliberately so:
    `gather`'s `cancel()` forwards into its children and leaves the awaiting
    task parked on a future that never completes (CPython #32684 semantics), so
    a `gather`-based drain could not be un-parked by any timeout — the bound
    must be structural. `wait`'s own timer completes the wait regardless of its
    children; a handler that ignores cancellation is reported by `wait` as
    still pending, never awaited again. Past the bound there is no clean path
    left inside the process, so it dumps every thread's stack and exits for a
    supervisor restart — the same last resort as the loop watchdog.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + drain_s
    pending = list(clients)
    for task in pending:
        task.cancel()
    if pending:
        _, still_pending = await asyncio.wait(pending, timeout=drain_s)
        if still_pending:
            dump_and_exit(f"shutdown cleanup did not finish within {drain_s:g}s")
    try:
        async with asyncio.timeout(max(deadline - loop.time(), 0.0)):
            await server.wait_closed()
    except TimeoutError:
        dump_and_exit(f"shutdown cleanup did not finish within {drain_s:g}s")


# meiva:note
# One request line as the daemon reads it; see `WireRequest`.
_WIRE_REQUEST = TypeAdapter(WireRequest)


class ComputerMcpDaemon(session.LocalTargetMixin):
    """Unix-socket server for the computer-mcp line protocol."""

    def __init__(
        self,
        config: ComputerUseConfig,
        db: Database,
        sock: str,
        *,
        helper: DesktopHelper | None,
        clock: Clock,
        home: Path,
        local_machine: str | None = None,
        gateway_access: GatewayAccess | None = None,
        user_home: Path | None = None,
        remote_helper_factory: Callable[[RemoteHostConfig, int | None], DesktopHelper]
        | None = None,
        cluster_tool_caller: ClusterToolCaller | None = None,
    ) -> None:
        self._db, self._helper, self._clock, self._home, self._sock = db, helper, clock, home, sock
        self._router: RemoteTransportRouter[DesktopHelper] = RemoteTransportRouter(
            db=db,
            clock=clock,
            home=home,
            local_machine=local_machine,
            gateway_access=gateway_access,
            user_home=user_home,
            remote_helper_factory=remote_helper_factory,
            cluster_tool_caller=cluster_tool_caller,
        )
        self._targets = session.TargetSessionRegistry(config, timers=clock)
        # Phase 2 audit envelope: task_id -> computer_session_start/end.
        self._task_sessions = TaskSessionTracker(idle_s=config.computer_use_session_idle_s)
        # Active client handler tasks, so shutdown can close them instead of
        # hanging in server.wait_closed() behind a client that never disconnects.
        self._clients: set[asyncio.Task[None]] = set()

    def _state_for(self, target_uri: str) -> session.TargetSessionState:
        return self._targets.state_for(target_uri)

    def close(self) -> None:
        self._router.close()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            with suppress(ConnectionResetError, BrokenPipeError):
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    try:
                        req = _WIRE_REQUEST.validate_json(line)
                    except ValueError as e:
                        resp: Response = {
                            "id": None,
                            "ok": False,
                            "error": f"JSON parse error: {e}",
                        }
                        writer.write((json.dumps(resp) + "\n").encode())
                        await writer.drain()
                        continue
                    resp = await self._dispatch(req)
                    writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode())
                    await writer.drain()
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    async def _dispatch(self, req: WireRequest) -> Response:
        req_id = req.get("id")
        if not isinstance(req_id, int) or isinstance(req_id, bool):
            return {"id": None, "ok": False, "error": "request id must be an integer"}
        method = req.get("method")
        if method == "ping":
            return {"id": req_id, "ok": True, "result": "pong"}
        if method == "status":
            return {
                "id": req_id,
                "ok": True,
                "result": {
                    "local_available": self._helper is not None,
                    "holder": self._targets.state_for("local").screen.holder,
                    "holders": self._targets.holders_snapshot(),
                },
            }
        if method == "list_tools":
            return {"id": req_id, "ok": True, "result": TOOLS}
        if method != "call_tool":
            return {"id": req_id, "ok": False, "error": f"unknown method {method!r}"}
        tool = req.get("tool") or ""
        raw_args: object = req.get("args") or {}
        agent_id = req.get("agent_id")
        if not isinstance(tool, str) or not is_object_dict(raw_args):
            return {
                "id": req_id,
                "ok": False,
                "error": "call_tool needs a 'tool' name and object 'args'",
            }
        if agent_id is not None and (not isinstance(agent_id, int) or isinstance(agent_id, bool)):
            return {"id": req_id, "ok": False, "error": "agent_id must be an integer"}
        return await self._call_tool(tool, raw_args, agent_id, req_id)

    async def _renew_and_note(
        self, state: session.TargetSessionState, agent_id: int, tool: str, args: dict[str, object]
    ) -> None:
        """A live caller renews the lease (success or failure — it is still acting on the
        screen) and its task session notes the action."""
        await state.screen.touch(agent_id)
        if args.get("task_id") is None:
            return
        tid = optional_int_field(args, "task_id")
        if tid is None:
            logger.warning(
                "[computer-mcp] task_id {!r} is not a whole number; no task session forms",
                args["task_id"],
            )
            return
        try:
            for event_type, session_agent_id, payload in self._task_sessions.note(
                tid, agent_id, tool, now=self._clock.now().timestamp()
            ):
                session.emit_audit_event(self._db, event_type, session_agent_id, payload)
        except Exception as e:
            logger.warning(f"[computer-mcp] task-session event failed: {e}")

    def _helper_for_target(self, target: ResolvedTarget, agent_id: int | None) -> DesktopHelper:
        if target.kind == "local":
            if self._helper is None:
                raise PermissionsHelperError(
                    "no local desktop available on this host (headless or permissions helper "
                    "disabled); pass host='meiva://<machine>' or host='ssh://<alias>' "
                    "(run `meiva computer list` to view available targets)"
                )
            return self._helper
        return self._router.ssh_helper_for(target.name, agent_id)

    async def _call_tool(
        self, tool: str, args: dict[str, object], agent_id: int | None, req_id: int
    ) -> Response:
        raw_host = args.get("host")
        try:
            target = self._router.resolve(raw_host)
        except (TargetResolutionError, RemoteHostConfigError) as exc:
            err_msg = str(exc)
            self._emit_action(
                agent_id, tool, args, "error", err_msg, target_host=str(raw_host or "local")
            )
            return {"id": req_id, "ok": False, "error": err_msg}
        tool_args = {k: v for k, v in args.items() if k != "host"}
        state = self._targets.state_for(target.canonical_uri)
        if tool == "release_control":
            return await self._release_control(
                agent_id, tool_args, req_id, target=target, state=state
            )
        if target.kind == "meiva":
            return await self._call_cluster_tool(
                target.name, target.canonical_uri, tool, tool_args, agent_id, req_id
            )
        if agent_id is not None and not await state.screen.acquire(
            agent_id, priority=priority(tool_args)
        ):
            busy = (
                f"screen busy on {target.canonical_uri}: held by agent {state.screen.holder or '?'} — "
                "wait for release_control or the lease to expire, then retry"
            )
            return {"id": req_id, "ok": False, "error": busy}
        async with state.action_lock:
            return await self._execute_locked(target, state, tool, tool_args, agent_id, req_id)

    async def _execute_locked(
        self,
        target: ResolvedTarget,
        state: session.TargetSessionState,
        tool: str,
        tool_args: dict[str, object],
        agent_id: int | None,
        req_id: int,
    ) -> Response:
        try:
            helper = self._helper_for_target(target, agent_id)
        except (PermissionsHelperError, TargetResolutionError, RemoteHostConfigError) as e:
            err = f"{type(e).__name__}: {e}"
            self._emit_action(
                agent_id, tool, tool_args, "error", err, target_host=target.canonical_uri
            )
            return {"id": req_id, "ok": False, "error": err}
        result, error, aborted = state.run_tool(
            helper,
            tool,
            tool_args,
            agent_id,
            stamp=snapshot_stamp(self._clock.now()),
            home=self._home,
        )
        if aborted:
            await state.screen.release(None)
            state.mouse_holder = None
            _hide_badge(helper)
        elif agent_id is not None:
            await self._renew_and_note(state, agent_id, tool, tool_args)
        outcome = "error" if error is not None else "ok"
        self._emit_action(
            agent_id,
            tool,
            tool_args,
            outcome,
            error,
            result=result,
            helper=helper,
            target_host=target.canonical_uri,
        )
        if error is not None:
            return {"id": req_id, "ok": False, "error": error}
        assert result is not None  # noqa: S101
        return {"id": req_id, "ok": True, "result": mcp_result(result)}

    async def _call_cluster_tool(
        self,
        machine: str,
        target_uri: str,
        tool: str,
        tool_args: dict[str, object],
        agent_id: int | None,
        req_id: int,
    ) -> Response:
        outcome, error, result = "ok", None, None
        try:
            result = await asyncio.to_thread(
                self._router.call_cluster_peer,
                machine,
                tool=tool,
                args=tool_args,
                agent_id=agent_id,
                stamp=snapshot_stamp(self._clock.now()),
            )
        except Exception as exc:
            outcome, error = "error", f"{type(exc).__name__}: {exc}"
        self._emit_action(
            agent_id,
            tool,
            tool_args,
            outcome,
            error,
            result=result,
            helper=None,
            target_host=target_uri,
        )
        if error is not None:
            return {"id": req_id, "ok": False, "error": error}
        assert result is not None  # noqa: S101
        return {"id": req_id, "ok": True, "result": mcp_result(result)}

    async def _release_control(
        self,
        agent_id: int | None,
        args: dict[str, object],
        req_id: int,
        *,
        target: ResolvedTarget,
        state: session.TargetSessionState,
    ) -> Response:
        if target.kind == "meiva":
            return await self._call_cluster_tool(
                target.name, target.canonical_uri, "release_control", args, agent_id, req_id
            )
        async with state.action_lock:
            force = bool(args.get("force"))
            helper = self._helper if target.kind == "local" else None
            released, mouse, outcome, error = await state.release_session(
                agent_id, force=force, helper=helper, target_uri=target.canonical_uri
            )
            self._emit_action(
                agent_id,
                "release_control",
                args,
                outcome,
                error,
                helper=helper,
                target_host=target.canonical_uri,
            )
            if released is not None and target.kind == "local" and helper is not None:
                _hide_badge(helper)
            elif target.kind == "ssh" and (released is not None or force):
                self._router.release_remote_ssh(target.name, force=force)
            if released is None:
                note = (
                    " (the mouse button you held was released)"
                    if mouse.get("mouse_released")
                    else ""
                )
                return {"id": req_id, "ok": False, "error": "not the screen holder" + note}
            body: dict[str, object] = {
                "released": released,
                "holder": state.screen.holder,
                "host": target.canonical_uri,
                **mouse,
            }
            return {"id": req_id, "ok": True, "result": mcp_result(body)}

    def _emit_action(
        self,
        agent_id: int | None,
        tool: str,
        args: dict[str, object],
        outcome: str,
        error: str | None,
        result: dict[str, object] | None = None,
        *,
        helper: DesktopHelper | None = None,
        target_host: str = "local",
    ) -> None:
        session.emit_computer_action(
            self._db,
            agent_id,
            tool,
            args,
            outcome,
            error,
            result,
            helper=helper,
            target_host=target_host,
        )


async def socket_in_use(path: Path) -> bool:
    """True when a live process is already listening on ``path``.

    A successful connect proves an occupant; ``FileNotFoundError`` /
    ``ConnectionRefusedError`` mean a stale socket (nobody listening) and are
    safe to unlink. Any other error is treated as occupied — fail closed
    rather than risk stealing a live instance's socket (same guard as
    browser-mcp; without it a second daemon spawned by a watchdog that
    misjudged the first dead steals the socket and orphans the live one).
    """
    try:
        reader, writer = await asyncio.open_unix_connection(path=path)
    except (FileNotFoundError, ConnectionRefusedError):
        return False
    except OSError:
        return True
    writer.close()
    with suppress(OSError):
        await writer.wait_closed()
    del reader  # nothing to close on a StreamReader; the writer close suffices
    return True


async def _serve_client(
    daemon: ComputerMcpDaemon, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Run one client connection (the tracked task's coroutine).

    A client's failure ends its connection, never the daemon the clients' group lives in.
    """
    try:
        await daemon.handle(reader, writer)
    except Exception:
        logger.exception("[computer-mcp] client handler failed")


def tracked_client(
    daemon: ComputerMcpDaemon,
    clients: asyncio.TaskGroup,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> asyncio.Task[None]:
    """Spawn the per-connection handler as a tracked task (cancellable at shutdown)."""
    task = clients.create_task(_serve_client(daemon, reader, writer))
    daemon._clients.add(task)
    task.add_done_callback(daemon._clients.discard)
    return task


async def run(
    sock: str,
    *,
    database: Callable[[], Database],
    helper: DesktopHelper | None,
    clock: Clock,
    home: Path,
    local_machine: str | None = None,
    gateway_access: GatewayAccess | None = None,
    user_home: Path | None = None,
) -> None:
    config = computer_use_config()
    daemon = ComputerMcpDaemon(
        config,
        database(),
        sock,
        helper=helper,
        clock=clock,
        home=home,
        local_machine=local_machine,
        gateway_access=gateway_access,
        user_home=user_home,
    )
    path = Path(daemon._sock)
    path.parent.mkdir(parents=True, exist_ok=True)
    if await socket_in_use(path):
        logger.error(
            "[computer-mcp] socket {} already served by a live daemon; refusing to start", path
        )
        raise SystemExit(1)
    with suppress(OSError):
        path.unlink()
    async with asyncio.TaskGroup() as clients:
        server = await asyncio.start_unix_server(
            lambda r, w: tracked_client(daemon, clients, r, w), path=str(path), limit=LINE_LIMIT
        )
        logger.info(f"[computer-mcp] listening on {path}")
        watchdog = guard_loop_liveness(
            asyncio.get_running_loop(), config.computer_use_loop_stall_s, timers=clock
        )
        stop = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with suppress(NotImplementedError):
                asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        try:
            await stop.wait()
        finally:
            watchdog.stop()
            server.close()
            try:
                await asyncio.wait_for(asyncio.to_thread(watchdog.join, 1.0), timeout=1.0)
            except TimeoutError:
                dump_and_exit("loop watchdog did not stop within 1.0s")
            if watchdog.is_alive():
                dump_and_exit("loop watchdog did not stop within 1.0s")
            await bounded_cleanup(
                server, daemon._clients, drain_s=config.computer_use_shutdown_drain_s
            )
            daemon.close()
            with suppress(OSError):
                path.unlink()
            logger.info("[computer-mcp] shutting down")


@dataclass(frozen=True)
class DaemonEntry:
    image: LoadedCommit
    machine: ConfiguredMachineIdentity
    gate: ProcessDbGate
    direct_url: ClusterDirectUrl


def prepare_entry(image: LoadedCommit, machine_config: ConfigBoot, *, home: Path) -> DaemonEntry:
    machine = ConfiguredMachineIdentity(machine_config)
    gate = ProcessDbGate(
        version=CodeVersion(image).get, process="computer-mcp", end_process=hard_exit
    )
    return DaemonEntry(
        image=image,
        machine=machine,
        gate=gate,
        direct_url=ClusterDirectUrl(machine.host, home=home),
    )


def main() -> None:
    image, machine_config = LoadedCommit.capture(), ConfigBoot()
    machine_config.boot()
    home = meiva_home()
    process_home = home.resolve()
    entry = prepare_entry(image, machine_config, home=process_home)
    database = lambda: Database.from_settings(gate=entry.gate, direct_url=entry.direct_url)  # noqa: E731
    clock = Clock(clock_config_from_boot(machine_config), timers=SYSTEM_TIMERS)
    pipeline = build_pipeline(
        database=database,
        machine=entry.machine,
        home=process_home,
        now=clock.now,
        otlp_enabled=lambda: machine_config.view.observability.telemetry_otlp_enabled,
        otlp_endpoint=lambda: machine_config.view.observability.telemetry_otlp_endpoint,
        rng=random.SystemRandom(),
    )
    init_gateway_process(
        name="computer-mcp",
        producer=lambda: pipeline,
        machine_reader=entry.machine.name,
        image=entry.image,
        home=process_home,
    )
    host = Host.system(environ=machine_config.process_environment())
    local_helper: DesktopHelper | None = (
        helper_client(home=process_home)
        if machine_config.view.services.permissions_helper_enabled
        and permissions_helper_incapability(host=host) is None
        else None
    )
    asyncio.run(
        run(
            str(computer_mcp_socket(home)),
            database=database,
            helper=local_helper,
            clock=clock,
            home=process_home,
            local_machine=entry.machine.name(),
            gateway_access=gateway_access_from_boot(machine_config),
            user_home=Path.home(),
        )
    )


if __name__ == "__main__":
    from base.log import prepare_process_logging

    prepare_process_logging()
    main()
