"""Machine-wide screen ownership for the computer-use daemon (Phase 2).

The desktop is one shared screen that multiple agents drive through the same
daemon. Phase 1 serialized individual actions; this module adds session-level
coordination so one agent's multi-step flow (snapshot -> think -> click -> ...)
is not interleaved by another agent's actions:

- **holder**: the agent that currently owns the screen. The first action from
  an idle screen implicitly acquires ownership — no separate acquire tool
  (Keep It Simple, per the Phase 2 design review).
- **lease**: every action renews the holder's lease (default 30s). A holder
  that stops acting for the lease duration loses ownership automatically — a
  hung session cannot pin the screen forever.
- **FIFO queue**: while someone else holds the screen, a request waits up to
  the queue timeout (default 30s) for its turn; on timeout it fails with a
  readable "screen busy" error instead of interleaving.
- **release**: explicit handover by the holder (`release_control` tool) or an
  operator kick (CLI, no identity) — the next waiter takes over.

This is resource coordination, not governance: nothing is refused based on
who the agent is (peer trust model, user ruling 2026-08-10). The daemon's
action lock still serializes individual executions; ownership decides WHO
may act, the lock decides WHEN.

Config via env (daemon is a per-machine service):
  MEIVA_COMPUTER_LEASE_S          (default 30)
  MEIVA_COMPUTER_QUEUE_TIMEOUT_S  (default 30)
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from base.agents.inbox.insert import emit_prepared_event, insert_inbound_message_in_transaction
from base.agents.messages.kwargs import NoteTag
from base.clock import Timers
from base.db import Database
from base.host.boundary.fields import optional_float_field, optional_int_field
from base.host.permissions_helper.client import PermissionsHelperError, UserAbortError
from base.log import logger
from base.telemetry import audit_events

from .audit_coords import audit_coords
from .ax_ids import AxSession
from .config import (
    DEFAULT_LEASE_S as DEFAULT_LEASE_S,
)
from .config import (
    LEASE_REMINDER_LEAD_SECONDS,
    NO_LOCAL_DESKTOP_ERR,
    ComputerUseConfig,
    clamp_lease_ttl_seconds,
    finalize_release_result,
)
from .errors import ComputerUseError
from .execute import execute_tool, priority
from .ocr import OcrCache
from .screen import DesktopHelper, measured_tool_scale
from .task_sessions import TaskSessionTracker

# Waiter poll interval: short enough that a released screen is re-taken
# promptly, long enough that idle waiters cost nothing.
_POLL_S = 0.05

DEFAULT_QUEUE_TIMEOUT_S = 30.0


def parse_lease_ttl_seconds(raw: object, *, default_s: float) -> int:
    parsed = clamp_lease_ttl_seconds(raw, default_s=default_s)
    if parsed is None:
        raise ComputerUseError("ttl_seconds must be a positive number of seconds")
    return parsed


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def send_lease_reminder_note(
    db: Database,
    agent_id: int,
    *,
    target_uri: str,
    remaining_seconds: int,
    expires_at_iso: str,
) -> None:
    content = (
        f"Desktop lease on {target_uri} expires in {remaining_seconds}s (at {expires_at_iso}). "
        f"Call acquire_control(host={target_uri!r}, ttl_seconds=...) to renew if you need more "
        f"time, or release_control(host={target_uri!r}) when finished."
    )
    try:
        with db.write_transaction() as conn, conn.cursor() as cur:
            _, prepared_event = insert_inbound_message_in_transaction(
                cur,
                agent_id,
                content,
                "system",
                kind="system_note",
                payload={"note_tag": NoteTag.SDK_HINT.value},
            )
        emit_prepared_event(prepared_event)
    except Exception:
        logger.opt(exception=True).warning(
            "[computer-mcp] failed to deliver lease reminder note to agent {}", agent_id
        )


class ScreenSession:
    """One holder + a FIFO of waiters, protected by a single state lock."""

    def __init__(
        self,
        timers: Timers,
        lease_s: float = DEFAULT_LEASE_S,
        queue_timeout_s: float = DEFAULT_QUEUE_TIMEOUT_S,
    ) -> None:
        self._timers = timers
        self._lease_s = lease_s
        self._queue_timeout_s = queue_timeout_s
        self._holder: int | None = None
        self._holder_ttl_s: float = lease_s
        self._generation: int = 0
        # meiva:note
        # The holder's lease deadline, on the timers this session was built with.
        self._lease_until: float = 0.0
        self._waiters: deque[tuple[asyncio.Future[bool], str]] = deque()
        self._state_lock = asyncio.Lock()

    @property
    def holder(self) -> int | None:
        """Current holder, for diagnostics (no lock — best effort)."""
        return self._holder

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def holder_ttl_s(self) -> float:
        return self._holder_ttl_s

    @property
    def lease_until(self) -> float:
        return self._lease_until

    def remaining_seconds(self, agent_id: int) -> float | None:
        if self._holder != agent_id:
            return None
        return max(0.0, self._lease_until - self._timers.monotonic())

    def _take(self, agent_id: int, now: float, *, ttl_s: float | None = None) -> None:
        """Become the holder (caller holds the state lock)."""
        effective_ttl = self._lease_s if ttl_s is None else ttl_s
        self._holder = agent_id
        self._holder_ttl_s = effective_ttl
        self._lease_until = now + effective_ttl
        self._generation += 1

    def _step(
        self,
        agent_id: int,
        fut: asyncio.Future[bool],
        priority: str,
        now: float,
        deadline: float,
        *,
        queued: bool,
        ttl_s: float | None = None,
    ) -> tuple[bool | None, bool]:
        """One acquire attempt (caller holds the state lock): `(verdict, queued)`.

        The verdict is True when we hold the screen, False on timeout, None to keep waiting.
        """
        free = self._holder is None or now >= self._lease_until
        if free:
            if self._waiters and self._waiters[0][0] is not fut:
                # The screen is free but someone queued before us —
                # join the tail and let them take it first.
                if not queued:
                    self._enqueue(fut, priority)
                    queued = True
                return None, queued
            if self._waiters:
                self._waiters.popleft()
            self._take(agent_id, now, ttl_s=ttl_s)
            return True, queued
        if self._holder == agent_id:
            if ttl_s is not None:
                self._take(agent_id, now, ttl_s=ttl_s)
            # Already the holder — pass through without re-queueing.
            return True, queued
        if not queued:
            self._enqueue(fut, priority)
            queued = True
        if now >= deadline:
            self._dequeue(fut)
            return False, queued
        return None, queued

    async def acquire(
        self, agent_id: int, priority: str = "normal", *, ttl_s: float | None = None
    ) -> bool:
        """Become the holder (or confirm we are), waiting FIFO up to the queue
        timeout. `priority="high"` jumps the queue (FIFO among highs — a P0
        task is not blocked behind a normal task's whole session). Returns
        False on timeout — the caller should fail the request with a busy
        error rather than interleaving on a held screen."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        queued = False
        deadline = self._timers.monotonic() + self._queue_timeout_s
        try:
            while True:
                async with self._state_lock:
                    verdict, queued = self._step(
                        agent_id,
                        fut,
                        priority,
                        self._timers.monotonic(),
                        deadline,
                        queued=queued,
                        ttl_s=ttl_s,
                    )
                if verdict is not None:
                    return verdict
                await self._timers.asleep(_POLL_S)
        except asyncio.CancelledError:
            # A cancelled waiter must not keep occupying its queue slot.
            if queued:
                async with self._state_lock:
                    self._dequeue(fut)
            raise

    def _enqueue(self, fut: asyncio.Future[bool], priority: str) -> None:
        """Join the waiters (caller holds the state lock): high priority
        inserts after the existing highs (FIFO among highs), normal appends."""
        if priority != "high":
            self._waiters.append((fut, "normal"))
            return
        insert_at = 0
        for i, (_f, p) in enumerate(self._waiters):
            if p != "high":
                break
            insert_at = i + 1
        self._waiters.insert(insert_at, (fut, "high"))

    def _dequeue(self, fut: asyncio.Future[bool]) -> None:
        """Remove a specific waiter (caller holds the state lock)."""
        for i, (f, _p) in enumerate(self._waiters):
            if f is fut:
                del self._waiters[i]
                return

    async def touch(self, agent_id: int, *, ttl_s: float | None = None) -> None:
        """Renew the holder's lease (a no-op for non-holders)."""
        async with self._state_lock:
            if self._holder == agent_id:
                effective_ttl = self._holder_ttl_s if ttl_s is None else ttl_s
                self._holder_ttl_s = effective_ttl
                self._lease_until = self._timers.monotonic() + effective_ttl
                self._generation += 1

    async def release(self, agent_id: int | None = None) -> int | None:
        """Release the screen. agent_id None = operator kick (any holder).
        Returns the released holder, or None when nobody was released."""
        async with self._state_lock:
            if self._holder is not None and (agent_id is None or self._holder == agent_id):
                released = self._holder
                self._holder = None
                self._lease_until = 0.0
                self._generation += 1
                return released
            return None

    def __repr__(self) -> str:
        return (
            f"ScreenSession(holder={self._holder}, "
            f"lease_until={self._lease_until:.1f}, waiters={len(self._waiters)})"
        )


def hide_badge(helper: DesktopHelper) -> None:
    try:
        helper.indicator_stop()
    except (PermissionsHelperError, OSError) as e:
        logger.warning(f"[computer-mcp] capture indicator not stopped, its lease will expire: {e}")


def _float_field(record: dict[str, object], key: str) -> float:
    value = optional_float_field(record, key)
    if value is None:
        raise TypeError(f"{key} is not a number")
    return value


@dataclass
class TargetSessionState:
    screen: ScreenSession
    action_lock: asyncio.Lock
    ax_session: AxSession
    ocr_cache: OcrCache
    pointer: tuple[float, float] | None = None
    scale: float | None = None
    mouse_holder: int | None = None
    reminder_due_at: float | None = None
    reminder_agent_id: int | None = None
    reminder_gen: int = 0

    def cancel_reminder(self) -> None:
        self.reminder_due_at = None
        self.reminder_agent_id = None
        self.reminder_gen = 0

    def lease_snapshot(self, agent_id: int, *, now: datetime) -> dict[str, object]:
        rem = self.screen.remaining_seconds(agent_id)
        remaining_s = 0.0 if rem is None else rem
        return {
            "ttl_seconds": round(self.screen.holder_ttl_s),
            "lease_remaining_seconds": max(0, round(remaining_s)),
            "lease_expires_at": _iso_utc(now + timedelta(seconds=remaining_s)),
        }

    def attach_in_band_lease_reminder(
        self,
        result: dict[str, object],
        agent_id: int | None,
        *,
        target_uri: str,
        now: datetime,
    ) -> None:
        if agent_id is None or self.screen.holder != agent_id:
            return
        if self.screen.holder_ttl_s <= LEASE_REMINDER_LEAD_SECONDS:
            return
        rem = self.screen.remaining_seconds(agent_id)
        if rem is None or rem > LEASE_REMINDER_LEAD_SECONDS:
            return
        rem_int = max(0, round(rem))
        expires_iso = _iso_utc(now + timedelta(seconds=rem))
        result["lease_expires_at"] = expires_iso
        result["lease_remaining_seconds"] = rem_int
        result["lease_reminder"] = (
            f"Desktop lease on {target_uri} expires in {rem_int}s (<=180s). "
            f"Call acquire_control(host={target_uri!r}, ttl_seconds=...) to renew, "
            f"or release_control(host={target_uri!r}) when done."
        )

    def arm_reminder(self, agent_id: int) -> None:
        self.cancel_reminder()
        if self.screen.holder != agent_id:
            return
        rem = self.screen.remaining_seconds(agent_id)
        if (
            self.screen.holder_ttl_s <= LEASE_REMINDER_LEAD_SECONDS
            or rem is None
            or rem <= LEASE_REMINDER_LEAD_SECONDS
        ):
            return
        self.reminder_due_at = self.screen.lease_until - LEASE_REMINDER_LEAD_SECONDS
        self.reminder_agent_id = agent_id
        self.reminder_gen = self.screen.generation

    async def acquire_implicit(
        self, agent_id: int | None, *, priority_name: str, target_uri: str
    ) -> tuple[bool, str | None]:
        if agent_id is None:
            return False, None
        gen_before = self.screen.generation
        if not await self.screen.acquire(agent_id, priority=priority_name):
            busy = (
                f"screen busy on {target_uri}: held by agent {self.screen.holder or '?'} — "
                "wait for release_control or the lease to expire, then retry"
            )
            return False, busy
        newly_acquired = self.screen.generation != gen_before
        if newly_acquired:
            self.arm_reminder(agent_id)
        return newly_acquired, None

    def take_due_reminder(self, *, now_mono: float) -> tuple[int, float] | None:
        due, aid, gen = self.reminder_due_at, self.reminder_agent_id, self.reminder_gen
        if due is None or aid is None or now_mono < due:
            return None
        self.cancel_reminder()
        if self.screen.holder != aid or self.screen.generation != gen:
            return None
        rem_now = self.screen.remaining_seconds(aid)
        if rem_now is None or rem_now <= 0:
            return None
        return aid, rem_now

    def track_effects(self, tool: str, args: dict[str, object], result: dict[str, object]) -> None:
        ocr_scale = self.ocr_cache.get("scale")
        self.scale = measured_tool_scale(
            tool, args, result, self.scale, ocr_scale, self.ax_session.visual_scale
        )
        if tool == "click_text":
            self.pointer = (_float_field(result, "x"), _float_field(result, "y"))
        if "frame" in args:
            return
        if tool in {"click", "move"} or (
            tool in {"scroll", "mouse_down", "mouse_up"} and "x" in args and "y" in args
        ):
            self.pointer = (_float_field(args, "x"), _float_field(args, "y"))
        elif tool == "drag":
            self.pointer = (_float_field(args, "end_x"), _float_field(args, "end_y"))

    def track_mouse_hold(self, tool: str, agent_id: int | None, result: dict[str, object]) -> None:
        if result.get("auto_released"):
            self.mouse_holder = None
        if tool == "mouse_down" and agent_id is not None:
            self.mouse_holder = agent_id
        elif tool == "mouse_up":
            self.mouse_holder = None

    def release_held_mouse(self, helper: DesktopHelper, why: str) -> dict[str, object]:
        holder = self.mouse_holder
        try:
            released = helper.mouse_release_all()["released"]
        except (PermissionsHelperError, OSError) as e:
            logger.error(
                f"[computer-mcp] could not release the mouse button held for agent {holder} ({why}): {e}"
            )
            return {"mouse_release_error": str(e)}
        self.mouse_holder = None
        if released:
            logger.warning(
                f"[computer-mcp] released the mouse button held for agent {holder} ({why}): {released}"
            )
        return {"mouse_released": released}

    def run_tool(
        self,
        helper: DesktopHelper,
        tool: str,
        tool_args: dict[str, object],
        agent_id: int | None,
        *,
        stamp: str,
        home: Path,
    ) -> tuple[dict[str, object] | None, str | None, bool]:
        if agent_id is not None and self.mouse_holder not in (None, agent_id):
            _ = self.release_held_mouse(helper, f"screen passed to agent {agent_id}")
        try:
            result = execute_tool(
                helper,
                tool,
                tool_args,
                agent_id or 0,
                pointer=self.pointer,
                scale=self.scale,
                ocr_cache=self.ocr_cache,
                ax_session=self.ax_session,
                stamp=stamp,
                home=home,
            )
            self.track_effects(tool, tool_args, result)
            self.track_mouse_hold(tool, agent_id, result)
        except UserAbortError as e:
            return None, f"{type(e).__name__}: {e}", True
        except Exception as e:
            return None, f"{type(e).__name__}: {e}", False
        return result, None, False

    def run_target_tool(
        self,
        get_helper: Callable[[], DesktopHelper],
        tool: str,
        tool_args: dict[str, object],
        agent_id: int | None,
        *,
        stamp: str,
        home: Path,
        helper_errors: tuple[type[Exception], ...],
        capture_app: bool,
    ) -> tuple[dict[str, object] | None, str | None, bool, str | None, DesktopHelper | None]:
        try:
            helper = get_helper()
        except helper_errors as e:
            return None, f"{type(e).__name__}: {e}", False, None, None
        result, error, aborted = self.run_tool(
            helper, tool, tool_args, agent_id, stamp=stamp, home=home
        )
        if aborted:
            hide_badge(helper)
        app: str | None = None
        if capture_app and agent_id is not None:
            with suppress(PermissionsHelperError, OSError):
                app = helper.frontmost_app()["app"] or None
        return result, error, aborted, app, helper

    async def release_session(
        self,
        agent_id: int | None,
        *,
        force: bool,
        helper: DesktopHelper | None,
        target_uri: str,
    ) -> tuple[int | None, dict[str, object], str, str | None]:
        released = await self.screen.release(None if force else agent_id)
        outcome, error = (
            ("ok", None) if released is not None else ("error", "not the screen holder")
        )
        if force and released is not None:
            logger.info(
                f"[computer-mcp] operator forced release of agent {released} on {target_uri}"
            )
        mouse: dict[str, object] = {}
        if (
            helper is not None
            and self.mouse_holder is not None
            and (released is not None or self.mouse_holder == agent_id)
        ):
            mouse = self.release_held_mouse(helper, "release_control")
            if "mouse_release_error" in mouse:
                outcome, error = "error", f"mouse not released: {mouse['mouse_release_error']}"
        return released, mouse, outcome, error


class TargetSessionRegistry:
    def __init__(self, config: ComputerUseConfig, *, timers: Timers) -> None:
        self._config = config
        self._timers = timers
        self._states: dict[str, TargetSessionState] = {"local": self._create()}

    def _create(self) -> TargetSessionState:
        return TargetSessionState(
            screen=ScreenSession(
                timers=self._timers,
                lease_s=self._config.computer_use_lease_s,
                queue_timeout_s=self._config.computer_use_queue_timeout_s,
            ),
            action_lock=asyncio.Lock(),
            ax_session=AxSession(),
            ocr_cache={},
        )

    def state_for(self, target_uri: str) -> TargetSessionState:
        state = self._states.get(target_uri)
        if state is None:
            state = self._create()
            self._states[target_uri] = state
        return state

    @property
    def default_lease_s(self) -> float:
        return self._config.computer_use_lease_s

    def holders_snapshot(self) -> dict[str, int | None]:
        return {uri: st.screen.holder for uri, st in self._states.items()}

    def cancel_all_reminders(self) -> None:
        for st in self._states.values():
            st.cancel_reminder()

    async def flush_due_reminders(
        self, *, db: Database, now_fn: Callable[[], datetime]
    ) -> float | None:
        now_mono, soonest = self._timers.monotonic(), None
        for uri, st in list(self._states.items()):
            due = st.take_due_reminder(now_mono=now_mono)
            if due is not None:
                aid, rem = due
                await asyncio.to_thread(
                    send_lease_reminder_note,
                    db,
                    aid,
                    target_uri=uri,
                    remaining_seconds=max(1, round(rem)),
                    expires_at_iso=_iso_utc(now_fn() + timedelta(seconds=rem)),
                )
            elif st.reminder_due_at is not None:
                delay = max(0.0, st.reminder_due_at - now_mono)
                soonest = delay if soonest is None else min(soonest, delay)
        return soonest

    async def run_reminder_loop(
        self, stop: asyncio.Event, *, db: Database, now_fn: Callable[[], datetime]
    ) -> None:
        while not stop.is_set():
            try:
                soonest = await self.flush_due_reminders(db=db, now_fn=now_fn)
            except Exception:
                logger.opt(exception=True).warning(
                    "[computer-mcp] lease reminder loop iteration failed"
                )
                soonest = None
            wait_s = 1.0 if soonest is None else min(soonest, 1.0)
            try:
                await self._timers.asleep(wait_s)
            except asyncio.CancelledError:
                return


async def acquire_target_control(
    state: TargetSessionState,
    *,
    agent_id: int | None,
    args: dict[str, object],
    target_kind: str,
    target_uri: str,
    local_available: bool,
    acquire_remote: Callable[[int], dict[str, object]] | None,
    default_lease_s: float,
    now_fn: Callable[[], datetime],
) -> tuple[str, str | None, dict[str, object] | None]:
    if agent_id is None:
        return "error", "acquire_control requires an agent_id", None
    try:
        ttl_seconds = parse_lease_ttl_seconds(args.get("ttl_seconds"), default_s=default_lease_s)
    except ComputerUseError as exc:
        return "error", str(exc), None
    if target_kind == "local" and not local_available:
        return "error", NO_LOCAL_DESKTOP_ERR, None
    was_holder = state.screen.holder == agent_id
    if not await state.screen.acquire(agent_id, priority=priority(args), ttl_s=float(ttl_seconds)):
        busy = (
            f"screen busy on {target_uri}: held by agent {state.screen.holder or '?'} — "
            "wait for release_control or the lease to expire, then retry"
        )
        return "error", busy, None
    async with state.action_lock:
        if target_kind == "ssh" and acquire_remote is not None:
            try:
                _ = await asyncio.to_thread(acquire_remote, ttl_seconds)
            except UserAbortError as exc:
                state.cancel_reminder()
                _ = await state.screen.release(None)
                state.mouse_holder = None
                return "error", f"{type(exc).__name__}: {exc}", None
            except Exception as exc:
                if not was_holder:
                    state.cancel_reminder()
                    _ = await state.screen.release(agent_id)
                return "error", f"{type(exc).__name__}: {exc}", None
        state.arm_reminder(agent_id)
        body: dict[str, object] = {
            "acquired": True,
            "holder": agent_id,
            "host": target_uri,
            **state.lease_snapshot(agent_id, now=now_fn()),
        }
        return "ok", None, body


async def release_target_control(
    state: TargetSessionState,
    *,
    agent_id: int | None,
    args: dict[str, object],
    target_kind: str,
    target_uri: str,
    helper: DesktopHelper | None,
    release_remote: Callable[[], tuple[bool, str | None]] | None,
) -> tuple[str, str | None, dict[str, object] | None, str | None]:
    async with state.action_lock:
        force = bool(args.get("force"))
        local = await state.release_session(
            agent_id, force=force, helper=helper, target_uri=target_uri
        )
        released = local[0]
        if released is not None:
            state.cancel_reminder()
        remote: tuple[bool, str | None] = (False, None)
        if released is not None and target_kind == "local" and helper is not None:
            hide_badge(helper)
        elif (
            target_kind == "ssh" and release_remote is not None and (released is not None or force)
        ):
            remote = await asyncio.to_thread(release_remote)
        return finalize_release_result(
            local,
            force=force,
            remote=remote,
            holder=state.screen.holder,
            target_uri=target_uri,
        )


class LocalTargetMixin:
    _targets: TargetSessionRegistry

    @property
    def _screen(self) -> ScreenSession:
        return self._targets.state_for("local").screen

    @property
    def _ocr_cache(self) -> OcrCache:
        return self._targets.state_for("local").ocr_cache

    @property
    def _pointer(self) -> tuple[float, float] | None:
        return self._targets.state_for("local").pointer

    @_pointer.setter
    def _pointer(self, value: tuple[float, float] | None) -> None:
        self._targets.state_for("local").pointer = value

    @property
    def _scale(self) -> float | None:
        return self._targets.state_for("local").scale

    @_scale.setter
    def _scale(self, value: float | None) -> None:
        self._targets.state_for("local").scale = value

    @property
    def _mouse_holder(self) -> int | None:
        return self._targets.state_for("local").mouse_holder

    @_mouse_holder.setter
    def _mouse_holder(self, value: int | None) -> None:
        self._targets.state_for("local").mouse_holder = value


def emit_audit_event(
    db: Database, event_type: str, agent_id: int, payload: dict[str, object]
) -> None:
    from base import telemetry
    from base.telemetry.audit_events import record_audit

    event = audit_events.prepare_event_log(
        event_type=event_type, agent_id=agent_id, source=f"agent:{agent_id}", payload=payload
    )
    with db.write_transaction() as conn:
        tagged = record_audit(conn, event)
    telemetry.emit_prepared(tagged)


def emit_computer_action(
    db: Database,
    agent_id: int | None,
    tool: str,
    args: dict[str, object],
    outcome: str,
    error: str | None,
    result: dict[str, object] | None = None,
    *,
    helper: DesktopHelper | None = None,
    app_name: str | None = None,
    target_host: str = "local",
) -> None:
    coords = audit_coords(tool, args, result)
    if agent_id is None:
        return
    app = app_name
    if app is None and helper is not None:
        with suppress(PermissionsHelperError, OSError):
            app = helper.frontmost_app()["app"] or None
    emit_audit_event(
        db,
        "computer_action",
        agent_id,
        {
            "action": tool,
            "app": app,
            "outcome": outcome,
            "error": error,
            "coords": coords,
            "path": result.get("path") if tool == "snapshot" and result else None,
            "task_id": args.get("task_id"),
            "target_host": target_host,
        },
    )


def note_task_session(
    tracker: TaskSessionTracker,
    db: Database,
    agent_id: int,
    tool: str,
    args: dict[str, object],
    *,
    now_ts: float,
) -> None:
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
        for event_type, session_agent_id, payload in tracker.note(tid, agent_id, tool, now=now_ts):
            emit_audit_event(db, event_type, session_agent_id, payload)
    except Exception as e:
        logger.warning(f"[computer-mcp] task-session event failed: {e}")
