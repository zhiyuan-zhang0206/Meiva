from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path

from base.host.boundary.json_values import is_object_dict
from base.host.env.harness_binaries import resolve_harness_binary
from base.lm.harness.antigravity.config import AntigravityConfig
from base.lm.harness.antigravity.mapping import (
    answer_texts,
    assistant_text,
    map_init,
    map_step_update,
    parse_result,
    parse_usage,
)
from base.lm.harness.antigravity.reconciliation.queue_files import agy_home, reconcile_inputs
from base.lm.harness.antigravity.trajectory import (
    PhantomAction,
    PhantomTurn,
    Route,
    Trajectory,
    phantom_action,
    route,
)
from base.lm.harness.antigravity.transport import AgyTransport, TransportClosedError
from base.lm.harness.base_session import (
    EVENT_STREAM_END,
    ActiveTurn,
    BaseHarnessSession,
    SessionBinding,
    SessionClock,
)
from base.lm.harness.cancel import CancelReport, unobserved_cancel
from base.lm.harness.delivery import HarnessInput, InputPhase, Reconciliation, validated_batch
from base.lm.harness.events import (
    AckSignal,
    AssistantText,
    HarnessCause,
    InterruptConfirmed,
    SessionFailed,
    TurnCompleted,
    TurnOrigin,
    TurnStarted,
    UnsolicitedOutput,
    Usage,
)
from base.lm.harness.process import (
    ChildProbe,
    settle_close,
    snapshot,
    watch_tree,
)
from base.lm.harness.session import SessionClosedError, SessionTotals

TRAJECTORY_POLL_SECONDS = 0.5


# meiva:note
# One `agy` conversation as a turn engine. Same three boundaries as the Claude bridge — ordered
# inbound, one outbound stream, an assertable cancel — with one harness difference that shapes the
# whole file: an interrupt kills the `agy` process. `interrupt()` therefore retires the transport,
# and the next turn respawns it with `--conversation`, so the transcript survives even though the
# process does not.
# meiva:link base/lm/harness/antigravity/docs/antigravity.md
class AgySession(BaseHarnessSession[AntigravityConfig]):
    harness = "antigravity"

    def __init__(
        self,
        config: AntigravityConfig,
        *,
        environ: Mapping[str, str] | None = None,
        binding: SessionBinding,
        probe: ChildProbe | None = None,
        transport_factory: Callable[[], AgyTransport] | None = None,
        clock: SessionClock,
        home: Path | None = None,
    ) -> None:
        super().__init__(
            config,
            binding=binding,
            probe=probe,
            totals=SessionTotals(cost_usd=None),
            clock=clock,
        )
        self._environ = dict(environ) if environ else None
        # meiva:note
        # The user home the conversation transcript is read from; None is the real one.
        self._home = home
        self._factory = transport_factory or self._default_transport
        self._transport: AgyTransport | None = None
        self._retired: AgyTransport | None = None
        self._step_text: dict[int, list[str]] = {}
        # meiva:note
        # `agy` names no turns, so the only thing that can tell this session's result
        # from one it never asked for is whether it wrote a turn out and has not seen
        # that turn end. The name is set before the write, so a `result` that races
        # the send still has an owner (#5664).
        # meiva:link base/lm/harness/docs/attribution.md
        self._sent_turn: str | None = None
        self._turn_steps: list[Usage] = []
        self._consumed = 0
        self._floor = 0
        self._phantom: PhantomTurn | None = None
        self._acked_start = False
        self._last_turn: str | None = None

    def _read_records(
        self, session_id: str, keys: Sequence[str]
    ) -> dict[str, Reconciliation] | None:
        return reconcile_inputs(agy_home(self._home, self._environ), session_id, keys)

    async def start(self, *, resume: str | None = None) -> None:
        if self._transport is not None:
            raise RuntimeError("session already started")
        if self._closed:
            raise SessionClosedError("session is closed")
        resume = self._resolve_resume(resume)
        _ = self._begin_generation()
        transport = self._factory()
        await transport.start(resume=resume)
        self._transport = transport
        await self._enter_tasks()
        self._launch(transport)

    async def close(self, *, reason: str = "closed") -> None:
        if self._closed:
            return
        self._closed = True
        self._sent_turn = None
        self._phantom = None
        self._observed.clear()
        transport, self._transport = self._transport, None
        watched = watch_tree(self._probe, transport.pid if transport is not None else None)
        await self._close_tasks()
        if transport is not None:
            await transport.close()
        await settle_close(
            self._probe,
            watched,
            "antigravity",
            self.config.cancel_settle_seconds,
            agent_id=self.agent_id,
            clock=self._clock,
        )
        # meiva:note
        # After the child was reaped: `settle_close` above waited for its tree, and
        # `transport.close()` waited for the process itself, so this is final.
        self._exit_code = transport.returncode if transport is not None else None
        self._finish_close(reason)

    @property
    def cli_pid(self) -> int | None:
        return self._transport.pid if self._transport is not None else None

    # meiva:note
    # Hand a batch to agy, in order. Before each input the conversation is asked what it is doing
    # (`GetCascadeTrajectory`): a running one takes the input through `SendUserCascadeMessage`
    # carrying our `clientMessageId`; an idle one must not be sent to that way, because agy answers 200
    # and runs a turn that stdout never reports, so the input starts a turn on stdin instead, or waits
    # (a rejection, so the caller hands it over again) when our own turn has finished but its result is
    # not yet read. HTTP 200 and a stdin write are weak receipts;
    # the second acknowledgement is the user-input step: the trajectory step that carries our
    # `clientMessageId`, or for a turn started on stdin the first user-input frame on stdout. The
    # trajectory is the one live signal, so a reply of an unexpected shape fails the delivery.
    # meiva:link base/lm/harness/docs/delivery.md
    async def deliver(self, inputs: Sequence[HarnessInput]) -> None:
        batch = validated_batch(inputs)
        if self._closed:
            raise SessionClosedError("session is closed")
        for entry in batch:
            self._ledger = self._ledger.sent(entry.key)
        for entry in batch:
            await self._deliver_one(entry)

    async def _deliver_one(self, entry: HarnessInput) -> None:
        transport = await self._ensure_transport()
        snapshot_ = await transport.trajectory() if self._session_id else None
        decided = route(
            snapshot_.status if snapshot_ is not None else None,
            ticket_active=self._active is not None,
        )
        if decided is Route.STEER:
            await self._steer_input(transport, entry)
            return
        if decided is Route.QUEUE:
            self._apply(
                self._ledger.rejected(
                    entry.key, "the running turn has finished; hand the input over again"
                )
            )
            return
        if entry.images:
            self._apply(self._ledger.rejected(entry.key, "agy cannot start a turn with an image"))
            raise ValueError("Antigravity can carry an image only on a running turn")
        _ = self._start_input_turn(entry)

    async def _steer_input(self, transport: AgyTransport, entry: HarnessInput) -> None:
        media = [
            {"mimeType": image.mime_type, "inlineData": image.data_base64} for image in entry.images
        ]
        try:
            await transport.steer(
                entry.text,
                client_message_id=entry.key,
                media=media,
                conversation_id=self._session_id,
            )
        except Exception:
            self._apply(self._ledger.rejected(entry.key, "the SendUserCascadeMessage call failed"))
            raise
        self._apply(self._ledger.received(entry.key, AckSignal.WEAK))

    async def interrupt(self, *, turn_id: str | None = None) -> CancelReport:
        if turn_id is not None:
            raise NotImplementedError("Antigravity does not support foreground-only cancellation")
        ticket = await self._interrupt_target(turn_id)
        transport = self._transport
        if transport is None:
            return unobserved_cancel(turn_id=turn_id)
        if ticket is None:
            return unobserved_cancel(
                turn_id=turn_id, cli_pid=transport.pid, cli_alive=transport.alive
            )
        self._retired = transport
        self._close_phantom(None)
        return await transport.interrupt(turn_id=ticket.turn_id, baseline=self._baseline)

    def _default_transport(self) -> AgyTransport:
        # meiva:note
        # A bare `agy` resolves against the child's PATH, and a harness child is handed an
        # environment with none (#5491). Pin the CLI we resolved; the rest of the argv
        # prefix (a test's fake CLI, an operator's flags) rides unchanged.
        # meiva:link base/lm/harness/docs/harness.md
        resolved = resolve_harness_binary(
            self.config.executable[0], path_env=(self._environ or {}).get("PATH")
        )
        return AgyTransport(
            replace(self.config, executable=(resolved, *self.config.executable[1:])),
            environ=self._environ,
            probe=self._probe,
            clock=self._clock,
        )

    async def _ensure_transport(self) -> AgyTransport:
        if self._closed:
            raise SessionClosedError("session is closed")
        current = self._transport
        if current is not None and current is not self._retired and current.alive:
            return current
        if current is not None:
            self._transport = None
            await current.close()
        self._close_phantom(None)
        _ = self._begin_generation()
        transport = self._factory()
        await transport.start(resume=self._session_id)
        self._transport = transport
        self._retired = None
        self._launch(transport)
        return transport

    def _launch(self, transport: AgyTransport) -> None:
        generation = self.generation
        self._reader = self._spawn(self._read_loop(transport, generation))
        _ = self._spawn(self._watch(transport, generation))

    async def _begin(self, ticket: ActiveTurn) -> None:
        ticket.started_at = self._monotonic()
        self._step_text = {}
        self._turn_steps = []
        self._acked_start = False
        self._last_turn = ticket.turn_id
        key = ticket.input_key
        if self._ledger.phase(key) in (None, InputPhase.REJECTED, InputPhase.LOST):
            self._ledger = self._ledger.sent(key)
        try:
            transport = await self._ensure_transport()
            self._baseline = snapshot(self._probe, transport.pid)
            self._sent_turn = ticket.turn_id
            await transport.send(ticket.text)
            ticket.interrupt_ready.set()
            self._emit(TurnStarted(ticket.turn_id, TurnOrigin.INPUT, (key,), self.generation))
            self._apply(self._ledger.received(key, AckSignal.WEAK))
        except (TransportClosedError, OSError, RuntimeError) as exc:
            self._sent_turn = None
            self._apply(self._ledger.rejected(key, f"the write to agy failed: {exc}"))
            self._fail_turn(ticket, exc)

    async def _read_loop(self, transport: AgyTransport, generation: int) -> None:
        try:
            async for payload in transport.events():
                if self._ledger.accepts(generation):
                    self._handle(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._emit(SessionFailed(error=f"session stream failed: {exc}"))
            self._fail_active(exc)
        finally:
            await self._on_stream_end(transport)

    # meiva:note
    # A turn we can only see here. agy runs a turn for an input sent while the conversation was idle
    # and prints nothing on stdout until the next stdin input, then emits that turn's steps again and
    # folds its answer into the next `result`. The trajectory is polled while an input is
    # unsettled or such a turn is open: a running conversation with no turn of ours opens an observed
    # turn (`TurnStarted(HARNESS, QUEUED_INPUT_REVIVED)`), an idle one closes it with the answer
    # read from the transcript, and `_consumed` marks the steps already attributed so the replay
    # on stdout is ignored and the next turn's text is only its own.
    # meiva:link base/lm/harness/antigravity/docs/antigravity.md
    async def _watch(self, transport: AgyTransport, generation: int) -> None:
        while (
            self._ledger.accepts(generation) and self._transport is transport and not self._closed
        ):
            await self._clock.asleep(TRAJECTORY_POLL_SECONDS)
            if not self._session_id or not self._needs_poll():
                continue
            self._observe(await transport.trajectory())

    def _needs_poll(self) -> bool:
        unsettled = self._ledger.keys_in(InputPhase.SENT) + self._ledger.keys_in(
            InputPhase.RECEIVED
        )
        return bool(unsettled) or self._phantom is not None

    def _observe(self, snap: Trajectory) -> None:
        action = phantom_action(
            snap.status,
            ticket_active=self._active is not None,
            phantom_open=self._phantom is not None,
        )
        if action is PhantomAction.OPEN:
            self._open_phantom(snap)
        turn = self._active.turn_id if self._active is not None else self._phantom_id()
        turn = turn or self._last_turn
        if turn is not None:
            for client_id in snap.client_message_ids():
                if self._ledger.phase(client_id) in (InputPhase.SENT, InputPhase.RECEIVED):
                    self._apply(self._ledger.in_context(client_id, turn))
        if action is PhantomAction.CLOSE:
            self._close_phantom(snap)

    def _phantom_id(self) -> str | None:
        return self._phantom.turn_id if self._phantom is not None else None

    def _open_phantom(self, snap: Trajectory) -> None:
        keys = tuple(
            client_id
            for client_id in snap.client_message_ids()
            if self._ledger.phase(client_id) in (InputPhase.SENT, InputPhase.RECEIVED)
        )
        turn_id = self._mint_turn_id()
        self._phantom = PhantomTurn(turn_id=turn_id, first_step=self._consumed)
        self._observed.add(turn_id)
        self._emit(
            TurnStarted(
                turn_id,
                TurnOrigin.HARNESS,
                keys,
                self.generation,
                cause=HarnessCause.QUEUED_INPUT_REVIVED if keys else HarnessCause.UNKNOWN,
                detail="trajectory",
            )
        )

    def _close_phantom(self, snap: Trajectory | None) -> None:
        phantom, self._phantom = self._phantom, None
        if phantom is None:
            return
        self._last_turn = phantom.turn_id
        texts = (
            answer_texts(self._session_id, phantom.first_step, home=self._home)
            if self._session_id
            else ()
        )
        for text in texts:
            self._emit(AssistantText(turn_id=phantom.turn_id, text=text))
        if snap is not None:
            self._consumed = max(self._consumed, snap.num_steps)
            self._floor = self._consumed
        interrupted = snap is None
        self._emit(
            TurnCompleted(
                turn_id=phantom.turn_id,
                session_id=self._session_id,
                text="\n".join(texts),
                subtype="interrupted" if interrupted else "success",
                is_error=interrupted,
                interrupted=interrupted,
                stop_reason=None,
                num_turns=max(len(texts), 1),
                duration_ms=0,
                usage=Usage(),
                cost_usd=None,
                cumulative_cost_usd=self._totals.cost_usd,
                permission_denials=(),
                generation=self.generation,
            )
        )
        self._observed.discard(phantom.turn_id)

    async def _on_stream_end(self, transport: AgyTransport) -> None:
        if transport is not self._transport:
            await transport.close()
            return
        self._transport = None
        retired = transport is self._retired
        if retired:
            self._retired = None
        await transport.close()
        if self._closed or (retired and self._sent_turn is None):
            return
        error = "agy stream ended"
        self._emit(SessionFailed(error=error))
        self._fail_active(SessionClosedError(error))

    def _handle(self, payload: dict[str, object]) -> None:
        name = payload.get("event")
        if name == "init":
            event = map_init(payload)
            self._session_id = event.session_id or self._session_id
            self._model = event.model or self._model
            self._emit(event)
            self._bind()
            return
        if name == "result":
            self._handle_result_event(payload)
            return
        ticket = self._active
        if ticket is None or name != "step_update":
            return
        update = payload.get("step_update")
        if is_object_dict(update) and not self._replayed(update):
            self._handle_step(ticket, update)

    def _handle_step(self, ticket: ActiveTurn, update: dict[str, object]) -> None:
        self._ack_start(ticket)
        self._accumulate(update)
        for event in map_step_update(update, turn_id=ticket.turn_id, home=self._home):
            self._emit(event)
        if update.get("state") != "DONE":
            return
        if update.get("step_type") == "agent_response" and isinstance(update.get("usage"), dict):
            self._turn_steps.append(parse_usage(update.get("usage")))
        done = assistant_text(update, turn_id=ticket.turn_id, text=self._take_step_text(update))
        if done is not None and done.text:
            ticket.parts.append(done.text)
            self._emit(done)

    # meiva:note
    # A step with an index below `_floor` was already attributed (a phantom turn closed from the
    # trajectory); agy prints it again on stdout at the next stdin input, and it must not be read as
    # part of the turn that input starts. Any other step advances the mark.
    def _replayed(self, update: dict[str, object]) -> bool:
        index = update.get("step_index")
        if not isinstance(index, int):
            return False
        if index < self._floor:
            return True
        self._consumed = max(self._consumed, index + 1)
        return False

    # meiva:note
    # The second acknowledgement of a turn started on stdin: its `user_input` step reaches stdout when the
    # input became a message of the conversation. No frame carries our id there, so the first one after
    # the write is ours; a CLI that prints no user-input frame is acknowledged by the first activity of
    # the turn instead.
    def _ack_start(self, ticket: ActiveTurn) -> None:
        if self._acked_start:
            return
        self._acked_start = True
        self._apply(self._ledger.in_context(ticket.input_key, ticket.turn_id))

    def _accumulate(self, update: dict[str, object]) -> None:
        if update.get("step_type") != "agent_response":
            return
        index = update.get("step_index", 0)
        delta = update.get("text_delta")
        bucket = self._step_text.setdefault(index if isinstance(index, int) else 0, [])
        if isinstance(delta, str) and delta:
            bucket.append(delta)

    # meiva:note
    # A step that never reaches DONE — a turn cut short by a result that was not ours —
    # still streamed text the agent already saw, so it belongs in the reply rather than
    # in a bucket nobody reads (#5664).
    # meiva:link base/lm/harness/docs/attribution.md
    def _flush_step_text(self) -> str:
        leftover = "".join("".join(bucket) for _, bucket in sorted(self._step_text.items())).strip()
        self._step_text.clear()
        return leftover

    def _take_step_text(self, update: dict[str, object]) -> str:
        index = update.get("step_index", 0)
        bucket = self._step_text.pop(index if isinstance(index, int) else 0, [])
        return "".join(bucket).strip()

    # meiva:note
    # `agy` prints one `result` per turn it ran, and no frame says which turn: the only
    # evidence is whether this session wrote a turn out and has not seen it end. A
    # result without one is the CLI's own work — nobody here asked for it — so it
    # becomes unsolicited output instead of closing a turn that never ran it. Mid-turn
    # the two are indistinguishable, which is why the turn's reply text is built from
    # this ticket's own parts rather than from the frame (#5664).
    # meiva:link base/lm/harness/docs/attribution.md
    def _handle_result_event(self, payload: dict[str, object]) -> None:
        ticket = self._active
        if ticket is not None and self._sent_turn == ticket.turn_id:
            self._sent_turn = None
            self._handle_result(payload.get("result"), ticket)
            return
        self._sent_turn = None
        raw = payload.get("result")
        fields = parse_result(raw if is_object_dict(raw) else {}, home=self._home)
        self._unsolicited(UnsolicitedOutput(source="result", text=fields.text.strip()))

    def _handle_result(self, payload: object, ticket: ActiveTurn) -> None:
        if not self._acked_start:
            self._acked_start = True
            self._apply(self._ledger.in_context(ticket.input_key, ticket.turn_id))
        fields = parse_result(payload if is_object_dict(payload) else {}, home=self._home)
        text = "\n".join(ticket.parts).strip() or self._flush_step_text() or fields.text.strip()
        errors = (fields.error,) if fields.error else ()
        self._totals = self._totals.with_turn(fields.usage, None)
        if fields.session_id:
            self._session_id = fields.session_id
        steps = tuple(self._turn_steps)
        num_turns = max(len(steps), fields.num_turns, 1)
        self._emit(
            TurnCompleted(
                turn_id=ticket.turn_id,
                session_id=fields.session_id or self._session_id,
                text=text,
                subtype=fields.subtype,
                is_error=fields.is_error,
                interrupted=fields.interrupted,
                stop_reason=fields.status,
                num_turns=num_turns,
                duration_ms=fields.duration_ms,
                usage=fields.usage,
                cost_usd=None,
                cumulative_cost_usd=self._totals.cost_usd,
                permission_denials=fields.denied_actions,
                steps=steps,
                errors=errors,
                generation=self.generation,
            )
        )
        if fields.interrupted:
            self._emit(InterruptConfirmed(ticket.turn_id, self.generation))
        self._complete(ticket)


__all__ = [
    "EVENT_STREAM_END",
    "ActiveTurn",
    "AgySession",
    "CancelReport",
    "SessionClosedError",
]
