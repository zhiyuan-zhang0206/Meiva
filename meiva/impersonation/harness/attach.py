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
# one. A lifecycle self-call (compact, restart, terminate) is refused by the SDK's
# own `assert_self_action`, which requires the caller to own the agent's turn loop
# and an attachment never does; everything else that a caller might forbid —
# spawning agents, scheduling, spending on `understand` — is a review convention,
# not a runtime rule.
# meiva:link meiva/impersonation/harness/docs/harness-attach.meiva.okf.md

from __future__ import annotations

import dataclasses
import os
from collections.abc import Callable
from typing import Any, Self, cast

import psutil

from base.agents.context import MeivaContext
from base.agents.context.external_attachment import ExternalAttachment
from base.agents.context.identity import AgentIdentity, ExternalLease
from base.cluster.machine import MachineIdentity
from base.config.agent_pins import resolve_agent_config_pins
from base.host.env.agent_slices import PluginConfigView
from base.packages.plugins.config_view import resolve_agent_plugin_pins
from meiva.boot import boot_external_process, initialize_context, plugin_state_codec
from meiva.impersonation.harness.registration import (
    HarnessRegistration,
    RegistrationError,
    RegistrationStore,
    default_store,
    handle_digest,
)
from meiva.sdk_surface import process_context

__all_for_meiva__ = []
HANDLE_ENV = "MEIVA_HARNESS_HANDLE"


@dataclasses.dataclass(frozen=True, slots=True)
class ProcessProbe:
    is_host: Callable[[], bool]
    ambient: Callable[[], MeivaContext | None]
    in_exec_turn: Callable[[], bool]


PROCESS_PROBE = ProcessProbe(
    is_host=process_context.is_host_process,
    ambient=process_context.ambient_context,
    in_exec_turn=process_context.in_exec_turn,
)


def refuse_unless_attachable(probe: ProcessProbe = PROCESS_PROBE) -> MeivaContext | None:
    if probe.is_host():
        raise RuntimeError("a native agent runtime cannot attach a harness identity")
    bound = probe.ambient()
    identity = None if bound is None else bound.identity
    if identity is not None and identity.lease is not None:
        raise RuntimeError("this process already has an external attachment")
    if identity is not None and identity.agent_id is not None and identity.owns_loop:
        raise RuntimeError("a native agent runtime cannot attach a harness identity")
    if probe.in_exec_turn():
        raise RuntimeError("an exec turn cannot attach a harness identity")
    return bound


def descends_from(controller_pid: int, *, pid: int | None = None) -> bool:
    if controller_pid <= 0:
        return False
    current = os.getpid() if pid is None else pid
    if current == controller_pid:
        return True
    try:
        process = psutil.Process(current)
        return any(parent.pid == controller_pid for parent in process.parents())
    except psutil.Error:
        return False


class HarnessAttachment(ExternalAttachment):
    def __init__(
        self,
        handle: str,
        *,
        agent_id: int,
        session_id: str,
        registration: HarnessRegistration,
        store: RegistrationStore,
        ancestor_check: Callable[[int], bool],
    ) -> None:
        self.handle = handle
        self.session_id = session_id
        self._registration = registration
        self._store = store
        self._ancestor_check = ancestor_check
        self.agent_id = agent_id
        self.config: tuple[Any, PluginConfigView] | None = None
        self._machine: MachineIdentity
        self._database: Any = None
        self._clock_factory: Any = None
        bound = refuse_unless_attachable()
        super().__init__(identity=_RegistrationIdentity(self), codec=_RegistrationCodec(self))
        self.open(bound)

    def _prepare(self) -> None:
        boot = boot_external_process(self._prior_context)
        self._clock_factory = boot.clock_factory
        self._machine = boot.machine
        self._database = boot.database
        self._own_clients = boot.clients
        self._require_registration()
        self._bind_borrowed_context()
        initialize_context(
            process_context.current_context(),
            surface=False,
            config=boot.config,
            clock_factory=self._clock_factory,
            validate_state=_codec_module().plugin_state_schema,
        )

    def _require_registration(self) -> HarnessRegistration:
        registration = self._registration
        if not registration.attached:
            raise RegistrationError(
                f"harness session {registration.external_session_id} is {registration.status}"
            )
        if registration.expired():
            raise RegistrationError(
                f"harness session {registration.external_session_id} expired at "
                f"{registration.expires_at}"
            )
        machine = self._machine.name()
        if registration.machine != machine:
            raise RegistrationError(
                f"the harness session belongs to machine {registration.machine!r}; "
                f"this process runs on {machine!r}"
            )
        if not self._ancestor_check(registration.controller_pid):
            raise RegistrationError(
                "this process does not descend from the controller that started the "
                f"harness session (pid {registration.controller_pid})"
            )
        return registration

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
        if self._own_clients is None:
            raise RuntimeError("an unbound attachment has no owned clients")
        return MeivaContext(
            clients=self._own_clients, clock_factory=self._clock_factory, machine=self._machine
        )

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
        process_context.get_state_update().clear()

    def _restore_prior_context(self) -> None:
        if self._bound and self._prior_context is not None:
            process_context.bind_context(self._prior_context)
        elif self._bound:
            process_context.unbind_context()

    def _unbind_exec_turn(self) -> None:
        process_context.unbind_exec_turn()

    def close(self) -> None:
        super().close()
        self._store.delete(self.agent_id)

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


def attach(
    handle: str | None = None,
    *,
    agent_id: int | None = None,
    session_id: str | None = None,
    store: RegistrationStore | None = None,
) -> HarnessAttachment:
    resolved_handle = handle or os.environ.get(HANDLE_ENV)
    if not resolved_handle:
        raise RegistrationError(
            "meiva.impersonation.harness.attach needs the session handle: pass it, or run inside a "
            f"harness process started by the bridge, which exports {HANDLE_ENV}"
        )
    resolved_store = store if store is not None else default_store()
    registration = resolved_store.find_by_handle(resolved_handle)
    if registration is None:
        raise RegistrationError("no harness session matches this handle")
    if agent_id is not None and registration.agent_id != agent_id:
        raise RegistrationError(
            f"this handle belongs to agent {registration.agent_id}, not {agent_id}"
        )
    if session_id is not None and registration.external_session_id != session_id:
        raise RegistrationError("this handle belongs to another external session")
    return HarnessAttachment(
        resolved_handle,
        agent_id=registration.agent_id,
        session_id=registration.external_session_id,
        registration=registration,
        store=resolved_store,
        ancestor_check=descends_from,
    )


def registration_digest(handle: str) -> str:
    return handle_digest(handle)
