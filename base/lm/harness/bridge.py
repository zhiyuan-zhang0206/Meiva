from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from datetime import timedelta

from base.api_contracts.agent_contract import Harness
from base.lm.harness.base_session import SessionClock
from base.lm.harness.delivery import HarnessInput, Reconciliation, ReconciliationUnavailableError
from base.lm.harness.engine import HarnessSession, HarnessTurn, completed_turn
from base.lm.harness.events import (
    BridgeEvent,
    ContextCompacted,
    InputInContext,
    InputLost,
    InputRejected,
    SessionClosed,
    SessionFailed,
    SessionFaulted,
    SessionStarted,
    TurnCompleted,
    TurnFaulted,
)
from base.lm.harness.faults import fault_message
from base.lm.harness.handoff import Handoff, compaction_reminder
from base.lm.harness.pump import EventPump, Subscription
from base.lm.harness.registration import (
    ATTACHED,
    HarnessRegistration,
    RegistrationStore,
    SecureRandom,
    handle_digest,
)
from base.lm.harness.session import (
    BackgroundItem,
    BackgroundWork,
    BackgroundWorkReporting,
    LostBackgroundReporting,
    lost_work_note,
)
from base.log import logger


class HarnessTurnFaultedError(RuntimeError):
    def __init__(self, message: str, *, outcome: HarnessTurn | None = None) -> None:
        super().__init__(message)
        self.outcome = outcome


class HarnessClosedError(RuntimeError):
    pass


# meiva:note
# A turn that never got a session, or that the session refused to accept. It is a
# different failure from a turn that died mid-flight: nothing was sent, so there is no
# half-written transcript to reconcile — but from the graph's side both mean the same
# thing, and both settle the turn as a reported abort rather than a host crash.
# meiva:link base/lm/harness/docs/harness.md
class HarnessSetupError(RuntimeError):
    pass


# meiva:note
# The session could not take the batch: nothing was handed to the harness, so the inputs can go back to the
# queue and be delivered again, unlike a failure after the hand-over, where whether they arrived is unknown.
# meiva:link base/lm/harness/docs/delivery.md
class HarnessNotDeliveredError(HarnessSetupError):
    pass


class HarnessBridge:
    # meiva:note
    # The turn engine the graph hands a message to. It owns one session, started on the
    # first turn and resumed by the adapter from the registry, and it is the only
    # consumer of that session's event stream: the graph runs one turn at a time per
    # agent, so a second reader would split the stream rather than add one.
    # On a fresh session, the handoff document is co-delivered in the first turn's
    # submission rather than run as a separate prelude turn.
    # meiva:link agent/graph/docs/harness-turn.md
    def __init__(
        self,
        harness: Harness,
        agent_id: int,
        factory: Callable[[str], HarnessSession],
        *,
        machine: str = "",
        handoff: Handoff | None = None,
        registrations: RegistrationStore | None = None,
        registration_ttl_seconds: float,
        clock: SessionClock,
        handle_rng: SecureRandom,
        controller_pid: int,
        own_turn: Callable[[], None] | None = None,
    ) -> None:
        self.harness = harness
        self._own_turn = own_turn
        self._agent_id = agent_id
        self._factory = factory
        self._machine = machine
        self._handoff = handoff
        self._instructions_file = None if handoff is None else handoff.reference_file
        self._context_compacted = False
        self._registrations = registrations
        self._clock = clock
        self._handle_rng = handle_rng
        self._controller_pid = controller_pid
        if registration_ttl_seconds <= 0:
            raise ValueError(f"a harness registration must expire, got {registration_ttl_seconds}s")
        self._registration_ttl = timedelta(seconds=registration_ttl_seconds)
        self._created_at: str | None = None
        self._session: HarnessSession | None = None
        self._last_session: HarnessSession | None = None
        self._pump: EventPump | None = None
        self._pump_task: asyncio.Task[None] | None = None
        self._pump_group: asyncio.TaskGroup | None = None
        self._renew_task: asyncio.Task[None] | None = None
        self._inbound = asyncio.Event()
        self._handoff_delivered = False
        self._lost: tuple[BackgroundItem, ...] = ()
        self._opening_key: str | None = None
        self._model: str | None = None
        self._digest: str | None = None
        self._closed = False
        self._close_error: BaseException | None = None
        # meiva:note
        # The exit status of the session this bridge retired last (three states: None
        # while a session runs or none was ever started, 0 normal, non-zero abnormal).
        # Recorded on the agent row by the host at retirement (task #5616).
        self._exit_code: int | None = None

    @property
    def model(self) -> str | None:
        return self._model

    @property
    def context_compacted(self) -> bool:
        return self._context_compacted

    @property
    def exit_code(self) -> int | None:
        return self._exit_code

    @property
    def handoff(self) -> Handoff | None:
        return self._handoff

    def set_handoff(self, handoff: Handoff) -> None:
        self._handoff = handoff
        self._instructions_file = handoff.reference_file

    @property
    def session_id(self) -> str | None:
        return None if self._session is None else self._session.session_id

    # meiva:note
    # The generation of the process the bridge's session runs, 0 while there is none. It grows
    # across sessions (the registry record carries it), so a consumer can tell a fact of the current
    # process from a late one of an earlier process.
    # meiva:link base/lm/harness/docs/delivery.md
    @property
    def generation(self) -> int:
        return 0 if self._session is None else self._session.generation

    # meiva:note
    # Whether the CLI began a turn of its own that no subscriber has read yet: the graph enters a run for
    # it (the host wakes the agent through `own_turn`), and the subscription that run opens is primed
    # with the turn's events so far.
    # meiva:link base/lm/harness/docs/delivery.md
    @property
    def has_unpresented_turn(self) -> bool:
        return self._pump is not None and self._pump.has_unpresented_turn

    async def ensure(self) -> HarnessSession:
        if self._close_error is not None:
            raise HarnessClosedError(
                "the previous harness session could not close"
            ) from self._close_error
        if self._session is not None:
            return self._session
        if self._closed:
            raise HarnessClosedError("this harness bridge is closed")
        handle = base64.urlsafe_b64encode(self._handle_rng.randbytes(32)).rstrip(b"=").decode()
        self._digest = handle_digest(handle)
        self._register("")
        session = self._factory(handle)
        try:
            await session.start()
        except BaseException:
            await self._retire(session, "session start failed")
            raise
        self._exit_code = None
        self._session = session
        self._last_session = session
        await self._start_pump(session)
        session_model = getattr(session, "model", None)
        if isinstance(session_model, str) and session_model:
            self._model = session_model
        self._handoff_delivered = bool(getattr(session, "resumed", False))
        return session

    # meiva:note
    # The pump reads the session's event stream for as long as the session lives, so an event that
    # arrives with no turn being waited for is still read and observed. The bridge's own state
    # (the model name, the registration, whether the CLI compacted its context) follows the stream
    # here; a turn waiter, or any other caller of `subscribe`, only watches it.
    # meiva:link base/lm/harness/docs/delivery.md
    async def _start_pump(self, session: HarnessSession) -> None:
        pump = EventPump(session.events(), observe=self._observe, own_turn=self._own_turn)
        group = asyncio.TaskGroup()
        _ = await group.__aenter__()
        self._pump = pump
        self._pump_group = group
        self._pump_task = group.create_task(pump.run(), name=f"harness-pump-{self._agent_id}")
        if self._registrations is not None:
            self._renew_task = group.create_task(
                self._renew_registration(session), name=f"harness-registration-{self._agent_id}"
            )

    async def _stop_pump(self) -> None:
        task, group, renew = self._pump_task, self._pump_group, self._renew_task
        self._pump_task = self._pump_group = self._pump = self._renew_task = None
        if task is None or group is None:
            return
        _ = task.cancel()
        if renew is not None:
            _ = renew.cancel()
        with suppress(asyncio.CancelledError):
            await task
        if renew is not None:
            with suppress(asyncio.CancelledError):
                await renew
        await group.__aexit__(None, None, None)

    def _observe(self, event: BridgeEvent) -> None:
        if isinstance(event, SessionStarted):
            if event.model:
                self._model = event.model
            try:
                self._register(event.session_id)
            except OSError as exc:
                logger.warning(
                    "could not register harness session for agent {agent_id}: {error}",
                    agent_id=self._agent_id,
                    error=exc,
                )
        elif isinstance(event, ContextCompacted):
            self._context_compacted = True
        elif isinstance(event, InputInContext) and event.key == self._opening_key:
            self._handoff = None
            self._handoff_delivered = True
            self._context_compacted = False
            self._opening_key = None
            self._lost = ()
        elif isinstance(event, (InputRejected, InputLost)) and event.key == self._opening_key:
            self._opening_key = None

    # meiva:note
    # A session that cannot start is a setup failure, not a crash: the graph settles it as a reported
    # abort, the same way it settles a session that died mid-turn.
    async def _ensure_for_delivery(self) -> HarnessSession:
        try:
            return await self.ensure()
        except HarnessClosedError:
            raise
        except Exception as exc:
            raise HarnessSetupError(f"{self.harness.value} session could not start: {exc}") from exc

    # meiva:note
    # Watch the session's events from now on. Subscribe before handing anything to the session: the
    # subscription sees only what the pump reads after it exists.
    async def subscribe(self) -> Subscription:
        await self._ensure_for_delivery()
        pump = self._pump
        if pump is None:
            raise HarnessClosedError("this harness bridge is closed")
        return pump.subscribe()

    # meiva:note
    # The delivery operation of the engine: hand a batch to the foreground, in order. The first
    # input of a fresh session carries the handoff document, and the first input after the CLI
    # compacted its context carries the reminder to re-read the saved instructions and the caller's
    # note (the agent's unanswered questions). Results arrive as events on a subscription.
    async def deliver(self, inputs: Sequence[HarnessInput], *, compaction_note: str = "") -> None:
        batch = list(inputs)
        if not batch:
            raise ValueError("deliver requires at least one input")
        try:
            session = await self._ensure_for_delivery()
        except HarnessNotDeliveredError:
            raise
        except (HarnessClosedError, HarnessSetupError) as exc:
            raise HarnessNotDeliveredError(str(exc)) from exc
        self._register(session.session_id or "")
        batch[0] = HarnessInput(
            key=batch[0].key,
            text=self._opening_input(batch[0].key, batch[0].text, session, compaction_note),
            images=batch[0].images,
        )
        await session.deliver(batch)

    # meiva:note
    # What the harness's own durable record says about inputs handed to an earlier process. It reads
    # the record of the live session, or of the one just retired: the record is a file, so asking needs no
    # running CLI. Nothing is answered when no session was ever started on this bridge.
    # meiva:link base/lm/harness/docs/delivery.md
    async def reconcile(self, keys: Sequence[str]) -> Mapping[str, Reconciliation]:
        session = self._session or self._last_session
        if session is None:
            raise ReconciliationUnavailableError("no harness session was started to reconcile with")
        return await session.reconcile(list(keys))

    # meiva:note
    # Tell the live session about inputs a previous process handed over that the harness will deliver
    # by itself, so their later facts settle them. Nothing is handed to the CLI.
    def adopt(self, keys: Sequence[str]) -> None:
        if self._session is None:
            raise HarnessClosedError("this harness bridge has no live session to adopt inputs into")
        self._session.adopt(list(keys))

    # meiva:note
    # What a turn opens with. The first turn of a fresh session carries the handoff, unless the
    # text already starts with it. After the CLI compacted its context, the next turn instead
    # opens with a reminder to re-read the saved instructions: the first message may be a summary
    # by now. The reminder rides the opening input rather than a mid-turn steer, and a turn that
    # carries the full handoff needs none. Whatever rode the opening input counts as delivered only when
    # that input is in the model's context: a refused or lost input is handed over again with it.
    # meiva:link base/lm/harness/docs/harness.md
    def _opening_input(
        self, key: str, text: str, session: HarnessSession, compaction_note: str
    ) -> str:
        if self._opening_key not in (None, key):
            return text
        if self._handoff is not None and self._handoff_delivered:
            self._handoff = None
        if self._handoff is not None:
            rendered = self._handoff.render().strip()
            self._opening_key = key
            if not text.startswith(rendered):
                text = f"{rendered}\n\n{text}" if text else rendered
        elif self._context_compacted and self._instructions_file is not None and text:
            self._opening_key = key
            reminder = compaction_reminder(self._instructions_file)
            if compaction_note:
                reminder = f"{reminder}\n\n{compaction_note}"
            text = f"{reminder}\n\n{text}"
        return self._with_lost_work(key, text, session)

    # meiva:note
    # What the CLI lost when its process went away, told until an input carrying it is in the model's
    # context: a refused or lost input hands it over again with the next one. The session reads it back from its
    # record when it resumes (or hands over what an in-session respawn dropped), and this is the
    # one place that turns it into words, on the first input after the process was replaced. The
    # note rides the input like the compaction reminder does: there is no separate turn for it, and
    # an agent that does not need the work anymore simply ignores it.
    # meiva:link base/lm/harness/docs/background-work.md
    def _with_lost_work(self, key: str, text: str, session: HarnessSession) -> str:
        if self._opening_key not in (None, key):
            return text
        if isinstance(session, LostBackgroundReporting):
            self._lost = (*self._lost, *session.take_lost_background())
        note = lost_work_note(
            self._lost, harness=self.harness.value, now=self._clock.now().timestamp()
        )
        if note is None:
            self._lost = ()
            return text
        self._opening_key = key
        logger.info(
            "agent {agent_id}: {count} background item(s) lost with the {harness} CLI process",
            event="harness_background_work_lost",
            agent_id=self._agent_id,
            count=len(self._lost),
            harness=self.harness.value,
        )
        return f"{note}\n\n{text}" if text else note

    # meiva:note
    # What the live session is holding that closing it would drop (nothing when there is no
    # session, or when its adapter cannot tell). The host's idle TTL and LRU read this and leave
    # such a session alone while the cache has room for it.
    # meiva:link base/lm/harness/docs/background-work.md
    def background_work(self) -> BackgroundWork:
        session = self._session
        if session is None or not isinstance(session, BackgroundWorkReporting):
            return BackgroundWork()
        return session.background_work()

    # meiva:note
    # Interrupt whatever the foreground is running. The target is the session's own running turn,
    # named or not: a turn the CLI began on its own is as interruptible as one an input started.
    # Nothing is interrupted when there is no session.
    async def interrupt(self) -> object:
        session = self._session
        if session is None:
            return None
        return await session.interrupt(turn_id=None)

    # meiva:note
    # The host's "there is new inbound for this agent" wake, carried to a graph invocation that is
    # already running (it never reaches the idle claim loop). The graph waits on it with a low
    # frequency fallback, so a wake that is lost costs one fallback interval, never a stuck message.
    def notify_inbound(self) -> None:
        self._inbound.set()

    async def wait_inbound(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._inbound.wait(), timeout)
        except TimeoutError:
            return
        finally:
            self._inbound.clear()

    # meiva:note
    # A session that faults or closes is dropped rather than reused, and the next inbound rebuilds
    # one from the registry. `retire` is that operation for a caller that saw the fault on its
    # subscription; retiring twice, or with no session, does nothing.
    async def retire(self, reason: str) -> None:
        session = self._session
        if session is not None:
            await self._retire(session, reason)

    async def close(self, *, reason: str = "closed") -> None:
        if self._close_error is not None:
            raise HarnessClosedError(
                "the previous harness session could not close"
            ) from self._close_error
        if self._closed:
            return
        self._closed = True
        if self._session is not None:
            await self._retire(self._session, reason)
        else:
            self._retire_registration()

    # meiva:note
    # A session that faults or closes mid-turn is dropped rather than reused, and the next
    # inbound rebuilds one from the registry — the provider still holds the transcript, so a
    # crashed CLI costs one turn instead of every later one. Retirement is not `close`: the
    # bridge stays open for that next turn, which is why the two are separate calls.
    # meiva:link base/lm/harness/docs/harness.md
    async def _retire(self, session: HarnessSession, reason: str) -> None:
        if self._session is session:
            self._session = None
            self._handoff_delivered = False
            self._opening_key = None
            self._created_at = None
            await self._stop_pump()
        self._retire_registration()
        try:
            await session.close(reason=reason)
        except BaseException as error:
            self._close_error = error
            raise
        # meiva:note
        # Only a session that closed reports an exit status; a failed close leaves
        # the field at None rather than inventing one.
        self._exit_code = session.exit_code

    # meiva:note
    # Every row expires so a reused controller pid cannot inherit a crashed host's
    # bearer capability. A running turn renews its registration independently of provider
    # output. The identity check prevents an interrupt or close from resurrecting a row.
    # meiva:link base/lm/harness/docs/harness.md
    async def _renew_registration(self, session: HarnessSession) -> None:
        interval = self._registration_ttl.total_seconds() / 3
        while self._session is session:
            await self._clock.asleep(interval)
            if self._session is not session:
                return
            try:
                self._register(session.session_id or "")
            except OSError as exc:
                logger.warning(
                    "could not renew harness registration for agent {agent_id}: {error}",
                    event="harness_registration_renewal_failed",
                    agent_id=self._agent_id,
                    error=exc,
                )

    # meiva:note
    # A renewal preserves the handle, controller and creation time for this session.
    # meiva:link base/lm/harness/docs/harness.md
    def _register(self, external_session_id: str) -> None:
        if self._registrations is None or self._digest is None:
            return
        now = self._clock.now()
        self._created_at = self._created_at or now.isoformat()
        self._registrations.write(
            HarnessRegistration(
                agent_id=self._agent_id,
                harness=self.harness.value,
                external_session_id=external_session_id,
                machine=self._machine,
                controller_pid=self._controller_pid,
                handle_sha256=self._digest,
                created_at=self._created_at,
                expires_at=(now + self._registration_ttl).isoformat(),
                status=ATTACHED,
            )
        )

    def _retire_registration(self) -> None:
        if self._registrations is not None:
            self._registrations.delete(self._agent_id)


# meiva:note
# What an event means for whoever is waiting on the session, as the error it raises there, or None when
# it is no failure. A turn that ended in error (and was not interrupted) or a failure that names a turn is
# that turn's fault; a failure of the session itself or its closing is the session's, never a turn's
# verdict, and no reader may infer a turn from it (#5663). `outcome` carries what a faulted turn
# produced so its usage is still accounted.
# meiva:link base/lm/harness/docs/attribution.md
def fault_of_event(harness: Harness, event: BridgeEvent) -> Exception | None:
    if isinstance(event, TurnCompleted):
        if event.is_error and not event.interrupted:
            outcome = completed_turn(event, "")
            return HarnessTurnFaultedError(fault_message(harness.value, outcome), outcome=outcome)
        return None
    if isinstance(event, TurnFaulted):
        return HarnessTurnFaultedError(event.error)
    if isinstance(event, SessionFailed):
        return HarnessClosedError(f"harness session failed: {event.error}")
    if isinstance(event, SessionClosed):
        return HarnessClosedError(f"harness session closed: {event.reason}")
    if isinstance(event, SessionFaulted):
        # meiva:note
        # The retired shape, emitted by an adapter that has not migrated yet. It splits the same way:
        # naming a turn is turn-scoped, naming none is the session's own failure. It is checked last
        # because the two events above are also instances of it.
        # meiva:link base/lm/harness/docs/harness.md
        if event.turn_id is None:
            return HarnessClosedError(f"harness session failed: {event.error}")
        return HarnessTurnFaultedError(event.error)
    return None
