# meiva:note
# A provider harness runs the agent loop in its own process, so the handoff
# document is not enough: that process needs the SDK, and a bare
# `python -c "import meiva; ..."` fails on an uninitialized telemetry pipeline. It
# borrows the agent identity the way a takeover does, through the same kernel
# (`base/agents/context/external_attachment`), and differs only in what proves the
# identity: a harness registration handle instead of a takeover lease.
#
# There is no capability gate. The user ruled on 2026-10-19 that a stored
# declaration is a record, never a check, and reverting that gate on the takeover
# path means the harness path must not re-introduce the same abstraction under a
# new name. An attached harness agent therefore has the same rights as a native
# one. Closing the attachment never touches the registration row: the bridge owns
# that document, and a provider sandbox would refuse the write anyway.
#
# A native-loop self-call (compact, restart) is refused by the SDK's own
# `assert_self_action`, which requires the caller to own the agent's turn loop;
# `meiva.self.pause_heartbeat` and `meiva.self.terminate` operate on the attached
# identity so a harness agent can pause its heartbeat or end its own process when
# its role is complete. Everything else that a caller might forbid — spawning
# agents, scheduling, spending on `understand` — is a review convention, not a
# runtime rule.
# meiva:link meiva/self/harness/docs/harness-attach.meiva.okf.md

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass
from typing import Any, Self, assert_never, cast

import psutil

from base.agents.context import MeivaContext
from base.agents.context.external_attachment import ExternalAttachment
from base.agents.context.identity import AgentIdentity, ExternalLease
from base.clock import Clock
from base.cluster.machine import MachineIdentityReader
from base.config import ConfigBoot
from base.config.agent_pins import resolve_agent_config_pins
from base.host.env.agent_slices import PluginConfigView
from base.packages.plugins.config_view import resolve_agent_plugin_pins
from meiva.boot import (
    BorrowedBoot,
    ExternalBoot,
    OwnedBoot,
    bind_launched_context,
    borrow_external_process,
    initialize_external,
    own_external_process,
    plugin_state_codec,
)
from meiva.sdk_surface import process_context
from meiva.sdk_surface.pure.caller.attachability import (
    BoundIdentity,
    ProcessFacts,
    refuse_unless_attachable,
)
from meiva.self.harness.admission import (
    ProcessLineage,
    require_claimed,
    require_live_registration,
)
from meiva.self.harness.registration import (
    HarnessRegistration,
    RegistrationError,
    RegistrationStore,
)
from meiva.self.harness.store import default_store

__all_for_meiva__ = []


def _prior_process_context() -> MeivaContext | None:
    bound = process_context.bound_context()
    if bound is not None or process_context.is_host_process():
        return bound
    return bind_launched_context()


def _bound_identity(bound: MeivaContext | None) -> BoundIdentity | None:
    identity = None if bound is None else bound.identity
    if identity is None:
        return None
    return BoundIdentity(
        has_lease=identity.lease is not None,
        agent_id=identity.agent_id,
        owns_loop=identity.owns_loop,
    )


def attachable_context() -> MeivaContext | None:
    bound = _prior_process_context()
    refuse_unless_attachable(
        ProcessFacts(
            is_host=process_context.is_host_process(),
            in_exec_turn=process_context.in_exec_turn(),
            bound=_bound_identity(bound),
        ),
        subject="a harness identity",
    )
    return bound


def process_lineage(pid: int) -> ProcessLineage:
    try:
        ancestors = frozenset(parent.pid for parent in psutil.Process(pid).parents())
    except psutil.Error:
        ancestors = frozenset[int]()
    return ProcessLineage(pid=pid, ancestors=ancestors)


# meiva:note
# Everything one harness attachment needs from the process, resolved by `open_attachment`: the
# registration the handle names, the context it borrows (None for an independent attachment), the
# boot of the process around it, this process's pid and the clock that dates each admission. The
# pid does not change; the instant and the lineage are asked again at every SDK call.
@dataclass(frozen=True, slots=True)
class HarnessInputs:
    handle: str
    registration: HarnessRegistration
    bound: MeivaContext | None
    boot: ExternalBoot
    pid: int
    clock: Clock


class HarnessAttachment(ExternalAttachment):
    def __init__(self, inputs: HarnessInputs) -> None:
        self.handle = inputs.handle
        self.session_id = inputs.registration.external_session_id
        self._registration = inputs.registration
        self.agent_id = inputs.registration.agent_id
        self.config: tuple[Any, PluginConfigView] | None = None
        self._boot: ExternalBoot = inputs.boot
        self._machine: MachineIdentityReader = inputs.boot.machine
        self._database: Any = inputs.boot.database
        self._pid = inputs.pid
        self._clock = inputs.clock
        super().__init__(identity=_RegistrationIdentity(self), codec=_RegistrationCodec(self))
        boot = self._boot
        match boot:
            case OwnedBoot():
                self._own_clients = boot.clients
            case BorrowedBoot():
                self._own_clients = None
            case _:
                assert_never(boot)
        self.open(inputs.bound)

    def _prepare(self) -> None:
        self._require_registration()
        self._bind_borrowed_context()
        initialize_external(
            process_context.current_context(),
            self._boot,
            validate_state=_codec_module().plugin_state_schema,
        )

    def _require_registration(self) -> HarnessRegistration:
        return require_live_registration(
            self._registration,
            machine=self._machine.name(),
            lineage=process_lineage(self._pid),
            now=self._clock.now(),
        )

    def _validate(self) -> int:
        if self._closed:
            raise RegistrationError("harness attachment is closed")
        self._require_registration()
        return self.agent_id

    def _bind_borrowed_context(self) -> None:
        borrowed = ExternalLease(
            agent_id=self.agent_id, validate=self._validate, config=lambda: self.config
        )
        bound = self._prior_context
        own = (None if bound is None else bound.identity) or AgentIdentity(
            agent_id=None, owns_loop=True
        )
        process_context.bind_context(
            dataclasses.replace(
                bound or self._new_context(),
                identity=dataclasses.replace(own, lease=borrowed),
                sdk_capture=self,
            )
        )
        self._bound = True

    def _new_context(self) -> MeivaContext:
        boot = self._boot
        match boot:
            case OwnedBoot():
                return boot.new_context()
            case BorrowedBoot():
                raise RuntimeError("a borrowed attachment has no owned clients to build a context")
            case _:
                assert_never(boot)

    def _resolve_config(self, overlay: Any, birth: Any) -> None:
        from meiva.sdk_surface.installation import installed

        installation = installed()
        if installation is None:
            raise RuntimeError("the SDK surface was not installed for the attachment")
        self.config = (
            resolve_agent_config_pins(overlay, birth),
            PluginConfigView(
                installation.configs, resolve_agent_plugin_pins(overlay, installation.configs)
            ),
        )

    def _set_state(self, state: Any) -> None:
        process_context.set_state(state)
        process_context.set_state_update({})

    def _state_update(self) -> Any:
        return process_context.get_state_update()

    def _clear_state_update(self) -> None:
        update = process_context.get_state_update()
        if isinstance(update, dict):
            update.clear()

    def _restore_prior_context(self) -> None:
        if self._bound and self._prior_context is not None:
            process_context.bind_context(self._prior_context)
        elif self._bound:
            process_context.unbind_context()

    def _unbind_exec_turn(self) -> None:
        process_context.unbind_exec_turn()

    def close(self) -> None:
        super().close()

    def __enter__(self) -> Self:
        return super().__enter__()


class _RegistrationIdentity:
    def __init__(self, attachment: HarnessAttachment) -> None:
        self._attachment = attachment

    def require(self) -> Any:
        registration = self._attachment._require_registration()
        return {"agent_id": registration.agent_id, "applied_version": 0, "plugin_delta": []}

    def validate(self) -> int:
        return self._attachment._validate()

    def machine(self) -> str:
        return self._attachment._machine.name()

    def agent_id(self) -> int:
        return self._attachment.agent_id

    def delta_version(self) -> int:
        return 0

    def merge_delta(self, encoded: dict[str, str], *, expected_version: int) -> int:
        del encoded, expected_version
        return 0


class _RegistrationCodec:
    def __init__(self, attachment: HarnessAttachment) -> None:
        self._attachment = attachment

    def load_snapshot(self, agent_id: int) -> Any:
        return _codec_module().load_snapshot(agent_id, database=self._attachment._database())

    def plugin_state_schema(self, state: Any) -> Any:
        del state
        return _codec_module().plugin_state_schema

    def encode_plugin_delta(self, update: Any, state_cls: type[Any]) -> dict[str, str]:
        return cast("dict[str, str]", _codec_module().encode_plugin_delta(update, state_cls))

    def decode_plugin_delta(self, encoded: dict[str, str], state_cls: type[Any]) -> Any:
        return _codec_module().decode_plugin_delta(encoded, state_cls)

    def apply_plugin_delta(self, state: Any, delta: Any) -> None:
        _codec_module().apply_plugin_delta(state, delta)


def _codec_module() -> Any:
    return plugin_state_codec()


def resolve_registration(
    handle: str,
    *,
    agent_id: int | None = None,
    session_id: str | None = None,
    store: RegistrationStore,
) -> HarnessRegistration:
    return require_claimed(store.find_by_handle(handle), agent_id=agent_id, session_id=session_id)


# meiva:note
# The clock that dates every admission of an attachment: the owned boot's own clock, or the clock
# of the context a borrowing attachment shares. A borrowed context with no clock factory fails here,
# before any attachment is built.
def attachment_clock(bound: MeivaContext | None, boot: ExternalBoot) -> Clock:
    match boot:
        case OwnedBoot():
            return boot.clock
        case BorrowedBoot():
            if bound is None:
                raise RuntimeError("a borrowed boot needs the context it borrows")
            return bound.require_clock()
        case _:
            assert_never(boot)


def open_attachment(
    handle: str,
    *,
    agent_id: int | None = None,
    session_id: str | None = None,
) -> HarnessAttachment:
    """Build the attachment for one registration handle; the caller owns its lifetime."""
    registration = resolve_registration(
        handle, agent_id=agent_id, session_id=session_id, store=default_store()
    )
    bound = attachable_context()
    boot = own_external_process(ConfigBoot()) if bound is None else borrow_external_process(bound)
    try:
        return HarnessAttachment(
            HarnessInputs(
                handle=handle,
                registration=registration,
                bound=bound,
                boot=boot,
                pid=os.getpid(),
                clock=attachment_clock(bound, boot),
            )
        )
    except BaseException:
        if isinstance(boot, OwnedBoot):
            boot.clients.close()
        raise
