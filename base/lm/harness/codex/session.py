from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

from base.host.boundary.json_values import is_object_dict
from base.host.env.harness_binaries import resolve_harness_binary
from base.lm.harness.base_session import ActiveTurn, BaseHarnessSession, SessionBinding
from base.lm.harness.cancel import (
    CancelReport,
    Sweep,
    foreground_work,
    observed_cancel,
    spawned_since,
    unobserved_cancel,
)
from base.lm.harness.codex.admission import observed_turn_name, rejection_reason
from base.lm.harness.codex.auth import build_env
from base.lm.harness.codex.commands import OpenCommands
from base.lm.harness.codex.config import CodexConfig
from base.lm.harness.codex.fork import fork_thread_request
from base.lm.harness.codex.frames import (
    JsonObject,
    JsonRpcError,
    RequestParams,
    TransportClosedError,
    as_object,
)
from base.lm.harness.codex.mapping import (
    item_event,
    reasoning_delta,
    text_delta,
    usage_from,
)
from base.lm.harness.codex.reconciliation.rollout import codex_home, reconcile_inputs
from base.lm.harness.codex.requests import (
    INITIALIZED,
    OpenedThread,
    answer_request,
    initialize_request,
    interrupt_request,
    open_thread_request,
    steer_request,
    turn_start_request,
)
from base.lm.harness.codex.subagents import SubagentThreads
from base.lm.harness.codex.transport import AppServerTransport, StdioAppServer
from base.lm.harness.codex.turns import (
    Inbound,
    NotificationKind,
    TurnRecord,
    binding,
    error_report,
    inbound,
    item,
    settle,
    unclaimed_text,
)
from base.lm.harness.delivery import HarnessInput, InputPhase, Reconciliation, validated_batch
from base.lm.harness.events import (
    AckSignal,
    AssistantText,
    HarnessCause,
    InterruptConfirmed,
    SessionFailed,
    SessionStarted,
    TurnFaulted,
    TurnOrigin,
    TurnStarted,
    UnsolicitedOutput,
)
from base.lm.harness.process import (
    ChildProbe,
    settle_close,
    snapshot,
    terminate,
    tool_snapshot,
    wait_gone,
    watch_tree,
)
from base.lm.harness.registry import SessionRecord
from base.lm.harness.session import (
    EVENT_STREAM_END,
    BackgroundItem,
    BackgroundWork,
    SessionClock,
    SessionClosedError,
    SessionTotals,
)

# meiva:note
# The three boundaries are the ones every harness bridge has, expressed in
# app-server terms. Ordinary inbound waits for `turn/completed`. Foreground
# input uses `turn/steer` with an active-turn precondition. One outbound queue
# preserves control events and may drop text deltas. Cancellation is `turn/interrupt` plus an
# assertion that the commands the turn spawned are gone, and `settled=None` when
# the app-server pid is unobservable — never a silent pass.
# meiva:link base/lm/harness/codex/docs/codex.md
CANCEL_POLL_SECONDS = 0.02


# meiva:note
# The CLI this session spawns, resolved through the directories a harness installs into as well as
# PATH: the session environment it is handed need not carry a PATH at all, and a bare name would
# then resolve against the OS default (#5491).
# meiva:link base/lm/harness/docs/harness.md
def resolved_binary(
    config: CodexConfig, environ: Mapping[str, str], *, bin_dirs: Sequence[str] | None = None
) -> str:
    return resolve_harness_binary(config.binary, path_env=environ.get("PATH"), bin_dirs=bin_dirs)


async def list_models(
    binary: str, *, cwd: Path, environ: Mapping[str, str], timeout: float
) -> JsonObject:
    config = CodexConfig(cwd=cwd)
    server = StdioAppServer(
        config.command(binary),
        cwd=config.cwd,
        environ=build_env(environ),
        stderr=subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(timeout):
            await server.start()
            _ = await server.request(*initialize_request())
            await server.notify(INITIALIZED)
            return await server.request("model/list", {})
    finally:
        await server.close()


class CodexSession(BaseHarnessSession[CodexConfig]):
    harness = "codex"

    def __init__(
        self,
        config: CodexConfig,
        *,
        environ: Mapping[str, str],
        binding: SessionBinding,
        transport: AppServerTransport | None = None,
        probe: ChildProbe | None = None,
        clock: SessionClock,
    ) -> None:
        super().__init__(
            config,
            binding=binding,
            probe=probe,
            totals=SessionTotals(),
            clock=clock,
        )
        self._transport: AppServerTransport = transport or StdioAppServer(
            config.command(resolved_binary(config, environ)),
            cwd=config.cwd,
            environ=build_env(environ, api_key_mode=config.api_key_mode),
            request_timeout=config.startup_timeout_seconds,
        )
        self._home_environ = dict(environ)
        self._starting = False
        self._early_notifications: list[tuple[Inbound, JsonObject]] = []
        self._records: dict[str, TurnRecord] = {}
        self._delivery_lock = asyncio.Lock()
        self._interrupting: set[str] = set()
        self._started = False
        self._commands = OpenCommands()
        self._subagents = SubagentThreads()
        self._start_tree: dict[int, str] = {}

    def _read_records(
        self, session_id: str, keys: Sequence[str]
    ) -> dict[str, Reconciliation] | None:
        return reconcile_inputs(codex_home(self._home_environ), session_id, keys)

    async def start(self, *, resume: str | None = None) -> None:
        if self._started:
            raise RuntimeError("session already started")
        if self._closed:
            raise SessionClosedError("session is closed")
        _ = self._begin_generation()
        self._transport.on_notification(self._on_notification)
        self._transport.on_request(self._on_request)
        self._transport.on_closed(self._on_transport_closed)
        await self._transport.start()
        self._start_tree = snapshot(self._probe, self._transport.pid)
        await self._enter_tasks()
        self._started = True
        await self._call(*initialize_request())
        await self._transport.notify(INITIALIZED)
        resolved_resume = self._resolve_resume(resume)
        origin = self._fork_origin(resolved_resume)
        if origin is not None:
            resolved_resume = await self._clone_thread(origin)
        result = await self._call(
            *open_thread_request(
                resolved_resume,
                cwd=str(self.config.cwd),
                model=self.config.model,
                approval_policy=self.config.approval_policy,
                sandbox=self.config.sandbox,
            )
        )
        opened = OpenedThread.of(
            result, resume=resolved_resume, model=self.config.model, cwd=str(self.config.cwd)
        )
        self._session_id = opened.session_id
        self._model = opened.model
        # meiva:note
        # Record the opened thread identity and emit `SessionStarted`.
        self._emit(
            SessionStarted(
                session_id=opened.session_id, model=opened.model, cwd=opened.cwd, turn_id=None
            )
        )
        if origin is None:
            self._bind()

    # meiva:note
    # The clone's id is registered the moment the app-server answers, before the thread is
    # resumed: the only window left is a crash between that answer and the registry write, and
    # it leaves one thread nobody uses. The next start clones again and registers the new one;
    # an unused thread costs disk and is never resumed, and Codex offers no way to name the
    # clone in advance that would close the window.
    # meiva:link base/lm/harness/docs/fork.md
    async def _clone_thread(self, origin: SessionRecord) -> str:
        result = await self._call(*fork_thread_request(origin.session_id, cwd=str(self.config.cwd)))
        thread = result.get("thread")
        cloned = thread.get("id") if is_object_dict(thread) else None
        if not isinstance(cloned, str) or not cloned:
            raise RuntimeError("thread/fork answered without a thread id")
        self._session_id = cloned
        self._bind()
        return cloned

    async def close(self, *, reason: str = "closed") -> None:
        if self._closed:
            return
        self._persist_pending()
        self._closed = True
        watched = watch_tree(self._probe, self._transport.pid)
        self._commands = OpenCommands()
        await self._close_tasks()
        await self._transport.close()
        await settle_close(
            self._probe,
            watched,
            "codex",
            self.config.cancel_settle_seconds,
            agent_id=self.agent_id,
            clock=self._clock,
        )
        self._exit_code = self._transport.returncode
        self._finish_close(reason)

    @property
    def cli_pid(self) -> int | None:
        return self._transport.pid

    # meiva:note
    # A turn the server began on its own can be interrupted too: when no ticket of ours runs, the
    # running observed turn is the foreground. A caller that names a turn id must name that one.
    def _observed_interrupt(self, turn_id: str | None) -> str | None:
        name = self._observed_name()
        if name is None or name in self._interrupting:
            return None
        if turn_id is not None and self._ledger.local_turn(name) != turn_id:
            return None
        return name

    # meiva:note
    # An interrupt ends the turn's work, like a native agent's cancel: the commands the turn started
    # and still has running stop with it. `turn/interrupt` ends the turn but leaves the app-server's
    # `unified_exec` commands running, and the protocol has no request that stops one the model
    # started (`command/exec/terminate` answers only for a `command/exec` the client itself began,
    # and an item's `processId` is the app-server's own id, not an OS pid), so the adapter ends the
    # process tree: the tool processes that appeared since the turn began (`ChildProbe.tool_processes`),
    # SIGTERM then SIGKILL. A command of an earlier turn that outlived it (a background command, see
    # `commands.py`) was running at the baseline and is left alone, and so are the app-server's own
    # helpers, which are not tool processes. `settled` is whether that foreground work is gone.
    # meiva:link base/lm/harness/docs/harness.md
    async def interrupt(self, *, turn_id: str | None = None) -> CancelReport:
        ticket = await self._interrupt_target(turn_id)
        name = ticket.provider_turn_id if ticket is not None else self._observed_interrupt(turn_id)
        if name is None or self._session_id is None:
            return unobserved_cancel(turn_id=turn_id)
        pid = self._transport.pid
        turn_id = ticket.turn_id if ticket is not None else self._ledger.local_turn(name)
        observed = tool_snapshot(self._probe, pid)
        spawned = foreground_work(observed, self._baseline)
        started = self._monotonic()
        self._interrupting.add(name)
        try:
            await self._call(*interrupt_request(self._session_id, name))
        except Exception:
            self._interrupting.discard(name)
            if ticket is not None:
                ticket.interrupt_requested = False
            raise
        interrupt_ms = (self._monotonic() - started) * 1000.0
        if pid is None:
            return unobserved_cancel(turn_id=turn_id, interrupt_ms=interrupt_ms)
        stopping = self._monotonic()
        killed = await terminate(
            self._probe,
            [child for child in spawned if self._probe.is_alive(child)],
            grace_seconds=self.config.cancel_settle_seconds,
            poll_seconds=CANCEL_POLL_SECONDS,
            clock=self._clock,
        )
        gone, _ = await wait_gone(
            self._probe,
            spawned,
            timeout_seconds=self.config.cancel_settle_seconds,
            poll_seconds=CANCEL_POLL_SECONDS,
            clock=self._clock,
        )
        waited_ms = (self._monotonic() - stopping) * 1000.0
        return observed_cancel(
            turn_id=turn_id,
            interrupt_ms=interrupt_ms,
            pid=pid,
            cli_alive=self._probe.is_alive(pid),
            sweep=Sweep(
                observed=observed,
                spawned=spawned,
                gone=gone,
                waited_ms=waited_ms,
                alive=() if gone else [child for child in spawned if self._probe.is_alive(child)],
            ),
            killed=killed,
        )

    def _observed_name(self) -> str | None:
        for name, local in self._ledger.open_turns.items():
            if local in self._observed:
                return name
        return None

    def _steer_target(self) -> tuple[str, str] | None:
        thread_id = self._session_id
        if self._closed or self._starting or not thread_id:
            return None
        ticket = self._active
        if ticket is not None:
            if ticket.interrupt_requested or not ticket.provider_turn_id:
                return None
            return thread_id, ticket.provider_turn_id
        name = self._observed_name()
        if name is None or name in self._interrupting:
            return None
        return thread_id, name

    # meiva:note
    # Hand a batch to the foreground, in order. Each input either joins the running turn
    # (`turn/steer`, whichever turn is running — ours or one the server began) or, when none runs,
    # begins one (`turn/start`); both carry the input's key as `clientUserMessageId`, which the server
    # echoes on the `userMessage` item that puts it in the model's context. The receipt of the request
    # is the first acknowledgement, that item is the second, and a turn that ends without it has lost the
    # input. A refusal (`no active turn to steer`) is a fact about the input, so it becomes an event, and
    # once one input is refused the rest of the batch is refused with it, so order is never inverted.
    # meiva:link base/lm/harness/docs/delivery.md
    async def deliver(self, inputs: Sequence[HarnessInput]) -> None:
        batch = validated_batch(inputs)
        if self._closed:
            raise SessionClosedError("session is closed")
        for entry in batch:
            self._ledger = self._ledger.sent(entry.key)
        async with self._delivery_lock:
            for index, entry in enumerate(batch):
                if not await self._deliver_one(entry):
                    for later in batch[index + 1 :]:
                        self._apply(
                            self._ledger.rejected(later.key, "an earlier input was rejected")
                        )
                    return

    async def _deliver_one(self, item: HarnessInput) -> bool:
        ticket = self._active
        if ticket is not None:
            await ticket.interrupt_ready.wait()
        target = self._steer_target()
        if target is not None:
            return await self._steer_input(item, *target)
        if self._idle():
            return await self._start_input(item)
        self._apply(self._ledger.rejected(item.key, "the foreground is not accepting input"))
        return False

    async def _start_input(self, item: HarnessInput) -> bool:
        turn = self._start_input_turn(item)
        await turn.interrupt_ready.wait()
        return self._ledger.phase(item.key) is not InputPhase.REJECTED

    async def _steer_input(self, item: HarnessInput, thread_id: str, turn_name: str) -> bool:
        self._ledger = self._ledger.scoped(item.key, turn_name)
        try:
            response = await self._call(*steer_request(thread_id, turn_name, item))
        except JsonRpcError as exc:
            reason = rejection_reason(exc.code, exc.message)
            if reason is None:
                raise
            self._apply(self._ledger.rejected(item.key, reason))
            return False
        if response.get("turnId") != turn_name:
            raise RuntimeError("Codex steer response did not name the expected turn")
        self._apply(self._ledger.received(item.key, AckSignal.STRONG))
        return True

    async def _begin(self, ticket: ActiveTurn) -> None:
        ticket.started_at = self._monotonic()
        self._baseline = tool_snapshot(self._probe, self._transport.pid)
        key = ticket.input_key
        if self._ledger.phase(key) in (None, InputPhase.REJECTED, InputPhase.LOST):
            self._ledger = self._ledger.sent(key)
        item = HarnessInput(key=key, text=ticket.text, images=ticket.images)
        # meiva:note
        # `turn/started` can beat this response and supply the interrupt target.
        # Activity waits for the response to certify its native turn id.
        # meiva:link base/lm/harness/docs/attribution.md
        self._starting = True
        try:
            result = await self._call(
                *turn_start_request(
                    self._session_id,
                    item,
                    model=self.config.model,
                    reasoning_effort=self.config.reasoning_effort,
                )
            )
        except Exception as exc:
            self._starting_failed(ticket, key, exc)
            return
        finally:
            self._starting = False
            early, self._early_notifications = self._early_notifications, []
        if self._active is not ticket:
            return
        if not self._bind_turn(ticket, result, starting=True):
            self._fail_turn(ticket, RuntimeError("Codex turn/start response did not name a turn"))
            return
        name = ticket.provider_turn_id or ""
        self._ledger = self._ledger.bind_turn(name, ticket.turn_id).scoped(key, name)
        self._emit(TurnStarted(ticket.turn_id, TurnOrigin.INPUT, (key,), self.generation))
        self._apply(self._ledger.received(key, AckSignal.STRONG))
        for message, params in early:
            self._dispatch_notification(message, params)

    def _starting_failed(self, ticket: ActiveTurn, key: str, exc: Exception) -> None:
        if isinstance(exc, JsonRpcError):
            reason = rejection_reason(exc.code, exc.message)
            if reason is not None:
                self._apply(self._ledger.rejected(key, reason))
        self._fail_turn(ticket, exc)

    def _bind_turn(self, ticket: ActiveTurn, announce: RequestParams, *, starting: bool) -> bool:
        bound = binding(announce, previous=ticket.provider_turn_id, starting=starting)
        if bound is None:
            return False
        if bound.discard:
            # meiva:note
            # A name that changed mid-turn is the provider replaying another turn's
            # transcript: the partial state gathered under the old name is not ours.
            # meiva:link base/lm/harness/docs/attribution.md
            self._records.pop(ticket.turn_id, None)
        ticket.provider_turn_id = bound.name
        ticket.interrupt_ready.set()
        return True

    def _on_notification(self, method: str, params: JsonObject) -> None:
        self._subagents = self._subagents.learned(method, params, session_id=self._session_id)
        message = inbound(method, params, session_id=self._session_id)
        if message is None:
            self._on_subagent(method, params)
            return
        if self._starting and message.kind is not NotificationKind.TURN_STARTED:
            self._buffer_early_notification(message, params)
            return
        self._dispatch_notification(message, params)

    # meiva:note
    # A sub-agent's finished message is shown in the turn that is running when it arrives; with no
    # turn running (a sub-agent that outlives its parent's turn) it has nowhere to be attributed
    # and is not shown.
    # meiva:link base/lm/harness/docs/subagent-text.md
    def _on_subagent(self, method: str, params: JsonObject) -> None:
        local = self._running_turn_id()
        if local is None:
            return
        if (event := self._subagents.text(method, params, turn_id=local)) is not None:
            self._emit(event)
        for tool_event in self._subagents.tools(method, params, turn_id=local):
            self._emit(tool_event)

    def _buffer_early_notification(self, message: Inbound, params: JsonObject) -> None:
        if not message.turn_id or params.get("threadId") != self._session_id:
            return
        if (
            message.kind in (NotificationKind.TEXT_DELTA, NotificationKind.REASONING_DELTA)
            and len(self._early_notifications) >= self.config.max_buffered_events
        ):
            self._dropped_deltas += 1
            return
        self._early_notifications.append((message, params))

    def _dispatch_notification(self, message: Inbound, params: JsonObject) -> None:
        match message.kind:
            case NotificationKind.TEXT_DELTA:
                self._on_text_delta(message, params)
            case NotificationKind.REASONING_DELTA:
                self._on_reasoning_delta(message, params)
            case NotificationKind.ITEM_STARTED | NotificationKind.ITEM_COMPLETED:
                self._on_item(message, params)
            case NotificationKind.USAGE:
                self._on_usage(message, params)
            case NotificationKind.TURN_STARTED:
                self._on_turn_started(params)
            case NotificationKind.TURN_COMPLETED:
                self._on_turn_completed(message)
            case NotificationKind.ERROR:
                self._on_error(params)

    # meiva:note
    # The local turn id a provider turn name belongs to: the running ticket's own turn (it may still
    # hold the name only provisionally, see `_bind_turn`), or a turn the server began on its own that
    # `turn/started` made known. A name that is neither is another turn's speech and belongs to no
    # turn here, so a late frame of a turn that already ended never lands on the next one.
    # meiva:link base/lm/harness/docs/delivery.md
    def _attribute(self, name: str | None) -> str | None:
        ticket = self._claim(name)
        if ticket is not None:
            return ticket.turn_id
        return self._ledger.local_turn(name)

    def _on_text_delta(self, message: Inbound, params: JsonObject) -> None:
        local = self._attribute(message.turn_id)
        if local is None:
            return
        for event in text_delta(params, turn_id=local):
            self._emit(event)

    def _on_reasoning_delta(self, message: Inbound, params: JsonObject) -> None:
        local = self._attribute(message.turn_id)
        if local is None:
            return
        for event in reasoning_delta(params, turn_id=local):
            self._emit(event)

    def _on_item(self, message: Inbound, params: JsonObject) -> None:
        phase = "completed" if message.kind is NotificationKind.ITEM_COMPLETED else "started"
        self._commands = self._commands.seen(phase, message.turn_id, as_object(params.get("item")))
        self._persist_pending()
        local = self._attribute(message.turn_id)
        if local is None:
            return
        step = item(message.kind, params)
        if step.user_message_key is not None and params.get("threadId") == self._session_id:
            self._apply(self._ledger.in_context(step.user_message_key, local))
        if step.text is not None:
            record = self._turn_record(local).with_text(step.text, final=step.final)
            self._records[local] = record
            self._emit(AssistantText(turn_id=local, text=step.text))
        for event in item_event(params, phase=step.phase, turn_id=local):
            self._emit(event)

    def _on_usage(self, message: Inbound, params: JsonObject) -> None:
        local = self._attribute(message.turn_id)
        if local is None:
            return
        self._records[local] = self._turn_record(local).with_usage(usage_from(params))

    # meiva:note
    # An error the server will not retry, on a turn this session is not running, is
    # news about the provider rather than a verdict on our turn. It reaches the sink
    # instead of ending a wait it has nothing to do with (#5663).
    # meiva:link base/lm/harness/docs/attribution.md
    def _on_error(self, params: JsonObject) -> None:
        report = error_report(params)
        if report is None:
            return
        local = self._attribute(report.turn_id)
        if local is None:
            self._unsolicited(UnsolicitedOutput(source="error", text=report.text))
            return
        self._emit(TurnFaulted(turn_id=local, error=report.text))

    # meiva:note
    # `turn/started` names the turn the server is about to run, and it can arrive
    # before `turn/start` has answered. Only a turn this session is in the middle
    # of starting may provisionally take the name so interruption can target it. A turn
    # that starts while this session has none of its own running is the server's: a
    # background wake-up, a queued message. It is observed as a turn of its own, so its
    # text and tools are attributed to it and the next turn we submit waits for it.
    # meiva:link base/lm/harness/docs/delivery.md
    def _on_turn_started(self, params: JsonObject) -> None:
        ticket = self._active
        if ticket is not None:
            _ = self._bind_turn(ticket, params, starting=self._starting)
            return
        name = observed_turn_name(params)
        if name is None or name in self._ledger.open_turns or self._closed:
            return
        local = self._mint_turn_id()
        self._ledger = self._ledger.bind_turn(name, local)
        self._observed.add(local)
        self._baseline = tool_snapshot(self._probe, self._transport.pid)
        self._emit(
            TurnStarted(
                local,
                TurnOrigin.HARNESS,
                (),
                self.generation,
                cause=HarnessCause.UNKNOWN,
                detail="turn/started",
            )
        )

    def _on_turn_completed(self, message: Inbound) -> None:
        name = message.turn_id
        survived = message.turn.get("status") == "completed"
        self._commands = self._commands.turn_ended(name, survived=survived)
        self._persist_pending()
        ticket = self._claim(name)
        local = ticket.turn_id if ticket is not None else self._ledger.local_turn(name)
        if local is None:
            # meiva:note
            # A completion for a turn this session did not run: a turn an earlier session ran and
            # the served transcript replays, or work the server did without announcing it. It is
            # neither our boundary nor a failure, and its reply is content the agent has not
            # read — so it goes to the sink the way any other inbound does (#5663).
            # meiva:link base/lm/harness/docs/attribution.md
            self._unsolicited(
                UnsolicitedOutput(source="turn/completed", text=unclaimed_text(message.turn))
            )
            return
        provider_name = name or ""
        self._apply(self._ledger.lose_unsettled(provider_name))
        self._ledger, first = self._ledger.end_turn(provider_name)
        if first is None:
            return
        settlement = settle(
            turn_id=local,
            session_id=self._session_id,
            turn=message.turn,
            record=self._turn_record(local),
            totals=self._totals,
        )
        self._records.pop(local, None)
        self._totals = settlement.totals
        self._interrupting.discard(provider_name)
        completed = replace(settlement.completed, generation=self.generation)
        self._emit(completed)
        if completed.interrupted:
            self._emit(InterruptConfirmed(local, self.generation))
        if ticket is not None:
            self._complete(ticket)
        else:
            self._observed.discard(local)

    # meiva:note
    # A command item still `inProgress` is pending only while the process tree agrees: the
    # app-server's descendants beyond those it had when it started. An app-server whose tree
    # cannot be observed (no pid) is taken at its word, because forgetting a live command is the
    # failure this exists to prevent.
    # meiva:link base/lm/harness/codex/docs/codex.md
    def _pending_commands(self) -> tuple[BackgroundItem, ...]:
        items = self._commands.items
        pid = self._transport.pid
        if not items or pid is None:
            return items
        grown = spawned_since(snapshot(self._probe, pid), self._start_tree)
        return items if grown else ()

    def background_work(self) -> BackgroundWork:
        if self._closed:
            return BackgroundWork()
        return BackgroundWork((*self._pending_commands(), *self._observed_turn_items()))

    def _persist_pending(self) -> None:
        self._persist_background(self._pending_commands())

    def _on_request(self, method: str, params: JsonObject) -> JsonObject:
        answer = answer_request(method, params, decision=self.config.approval_decision)
        local = self._running_turn_id()
        if answer.denial is not None and local is not None:
            self._records[local] = self._turn_record(local).with_denial(answer.denial)
        return answer.answer

    def _running_turn_id(self) -> str | None:
        if self._active is not None:
            return self._active.turn_id
        name = self._observed_name()
        return self._ledger.local_turn(name)

    # meiva:note
    # The app-server is gone: no turn it ran can end any more and no acknowledgement can arrive. The
    # turns it began on its own are dropped with it. An input it had received but not put in the
    # context is not declared lost here — the process dying says nothing about whether the thread
    # recorded it — and is left for reconciliation against the rollout.
    # meiva:link base/lm/harness/docs/delivery.md
    def _on_transport_closed(self, reason: str) -> None:
        if self._closed:
            return
        self._observed.clear()
        self._emit(SessionFailed(error=f"app-server: {reason}"))
        self._fail_active(TransportClosedError(reason))

    def _turn_record(self, turn_id: str) -> TurnRecord:
        return self._records.get(turn_id) or TurnRecord()

    async def _call(self, method: str, params: RequestParams) -> JsonObject:
        return await self._transport.request(
            method, params, timeout=self.config.startup_timeout_seconds
        )


__all__ = [
    "CANCEL_POLL_SECONDS",
    "EVENT_STREAM_END",
    "ActiveTurn",
    "CancelReport",
    "CodexSession",
    "SessionClosedError",
    "SessionTotals",
]
