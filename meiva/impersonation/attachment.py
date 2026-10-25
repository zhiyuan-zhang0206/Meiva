# meiva:note
# A takeover attachment. What is lease-specific lives here — the derived lease id,
# the active-lease recheck on every call, the delta column's optimistic version and
# the durable event participant the takeover manifest owns. The kernel
# (`base.agents.context.external_attachment`) owns the sequence and the failure handling,
# so a second borrowed identity cannot drift from this one's ordering.
#
# `attach` is the facade: it reads the process once (the ambient context, the host and exec-turn
# facts) and hands `Attachment` explicit inputs. `Attachment` is the shell the kernel drives; the
# decisions it takes (which lease a call names, whether the lease still fits this machine and
# version, what the native checkpoint already applied, whether a receipt opens) are the strict
# functions of `admission.py`, called with the values the shell read.
# meiva:link meiva/impersonation/docs/attachment-contracts.meiva.okf.md

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, assert_never
from uuid import uuid4

from base.agents.context import MeivaContext
from base.agents.context.clients import ClientSet, DatabaseFactory
from base.agents.context.external_attachment import ExternalAttachment
from base.agents.context.external_attachment.state_schema import BaseAgentState
from base.agents.context.identity import AgentIdentity, ExternalLease
from base.agents.impersonation import merge_plugin_delta, require_active
from base.agents.impersonation.manifest import (
    LocalCaptureGate,
    LocalParticipant,
    begin_local_participant_close,
    capture_local_event,
    close_local_participant_admission,
    open_local_participant,
    seal_local_participant,
)
from base.cluster.machine import MachineIdentityReader
from base.config import ConfigBoot
from base.config.agent_pins import resolve_agent_config_pins
from base.config.service_read import ConfigAuthority
from base.host.env.agent_slices import PluginConfigView
from base.native_process.ownership import process_metadata
from base.packages.plugins.config_view import resolve_agent_plugin_pins
from base.telemetry import Event
from meiva.boot import (
    BorrowedBoot,
    ExternalBoot,
    OwnedBoot,
    borrow_external_process,
    initialize_external,
    own_external_process,
    plugin_state_codec,
)
from meiva.impersonation.admission import (
    LeaseTarget,
    applied_version,
    lease_target,
    participant_source_key,
    require_held_version,
)
from meiva.sdk_surface import process_context
from meiva.sdk_surface.installation import config_authority, installed
from meiva.sdk_surface.pure.caller.attachability import (
    BoundIdentity,
    ProcessFacts,
    refuse_unless_attachable,
)

__all__ = ["AttachInputs", "Attachment", "attach"]


# meiva:note
# Everything one attachment needs from the process, resolved by `attach`: the lease it names, the
# context it borrows (None for an independent attachment) and the boot of the process around it,
# which holds the clients, clock and configuration an independent attachment owns.
@dataclass(frozen=True, slots=True)
class AttachInputs:
    target: LeaseTarget
    bound: MeivaContext | None
    boot: ExternalBoot


class _Codec:
    def __init__(self, attachment: Attachment) -> None:
        self._attachment = attachment

    def load_snapshot(
        self, agent_id: int
    ) -> tuple[BaseAgentState, Mapping[str, Any] | None, Mapping[str, Any] | None]:
        return plugin_state_codec().load_snapshot(agent_id, database=self._attachment._database())

    def plugin_state_schema(self, state: BaseAgentState) -> object:
        del state
        return plugin_state_codec().plugin_state_schema

    def encode_plugin_delta(
        self, update: Mapping[str, Any], state_cls: type[Any]
    ) -> dict[str, str]:
        encoded: Any = plugin_state_codec().encode_plugin_delta(update, state_cls)
        return encoded

    def decode_plugin_delta(self, encoded: dict[str, str], state_cls: type[Any]) -> dict[str, Any]:
        return plugin_state_codec().decode_plugin_delta(encoded, state_cls)

    def apply_plugin_delta(self, state: BaseAgentState, delta: Mapping[str, Any]) -> None:
        plugin_state_codec().apply_plugin_delta(state, delta)


class _LeaseIdentity:
    def __init__(self, attachment: Attachment) -> None:
        self._attachment = attachment

    def require(self) -> Mapping[str, Any]:
        return self._attachment._lease()

    def validate(self) -> int:
        return self._attachment._validate()

    def machine(self) -> str:
        return self._attachment._machine.name()

    def agent_id(self) -> int:
        return self._attachment.agent_id

    def delta_version(self) -> int:
        return self._attachment._version

    def merge_delta(self, encoded: dict[str, str], *, expected_version: int) -> int:
        attachment = self._attachment
        merge_plugin_delta(
            attachment._database(),
            attachment.lease_id,
            encoded,
            expected_version=expected_version,
            local_machine=attachment._machine.name(),
        )
        attachment._version += 1
        return attachment._version


class Attachment(ExternalAttachment):
    def __init__(self, inputs: AttachInputs) -> None:
        self._target = inputs.target
        self.lease_id = inputs.target.lease_id
        self.config: tuple[Mapping[str, Any], PluginConfigView] | None = None
        self._seal_authority: ConfigAuthority | None = None
        self.agent_id: int = 0
        self.session_id: int = 0
        self._version: int = 0
        super().__init__(identity=_LeaseIdentity(self), codec=_Codec(self))
        self._boot: ExternalBoot = inputs.boot
        self._machine: MachineIdentityReader = self._boot.machine
        self._database: DatabaseFactory = self._boot.database
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
        boot = self._boot
        target = self._target
        if target.session_id is not None and target.agent_id is not None:
            from base.agents.impersonation.sessions import private_id

            self.lease_id = private_id(
                self._database(),
                target.agent_id,
                target.session_id,
                local_machine=self._machine.name(),
            )
        lease = self._lease()
        self.agent_id = int(lease["agent_id"])
        self.session_id = int(lease["session_id"])
        self._version = int(lease["delta_version"])
        self._bind_borrowed_context()
        initialize_external(
            process_context.current_context(),
            boot,
            validate_state=plugin_state_codec().plugin_state_schema,
        )
        match boot:
            case OwnedBoot():
                self._seal_authority = None
            case BorrowedBoot():
                self._seal_authority = config_authority()
            case _:
                assert_never(boot)

    def _applied_version(self, state: BaseAgentState, lease: Mapping[str, Any]) -> int:
        receipt = state.impersonation_applied
        lease_id = receipt.get("lease_id")
        version = receipt.get("version", 0)
        return applied_version(
            lease["applied_version"],
            lease_id if isinstance(lease_id, str) else None,
            version if isinstance(version, int) and not isinstance(version, bool) else 0,
            self.lease_id,
        )

    def _resolve_config(
        self, overlay: Mapping[str, Any] | None, birth: Mapping[str, Any] | None
    ) -> None:
        installation = installed()
        if installation is None:
            raise RuntimeError("the SDK surface was not installed for the attachment")
        self.config = (
            resolve_agent_config_pins(overlay, birth),
            PluginConfigView(
                installation.configs, resolve_agent_plugin_pins(overlay, installation.configs)
            ),
        )

    def _set_state(self, state: BaseAgentState) -> None:
        process_context.set_state(state)
        process_context.set_state_update({})

    def _state_update(self) -> object:
        return process_context.get_state_update()

    def _clear_state_update(self) -> None:
        update = process_context.get_state_update()
        if isinstance(update, dict):
            update.clear()

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

    def capture_local_event(self, event: Event) -> Event:
        return capture_local_event(event, gate=self._event_gate)

    def _lease(self) -> dict[str, Any]:
        return require_active(self._database(), self.lease_id, local_machine=self._machine.name())

    def _validate(self) -> int:
        if self._closed:
            raise RuntimeError("external attachment is closed")
        require_held_version(self._lease()["delta_version"], self._version)
        return self.agent_id

    def _telemetry_clients(self) -> ClientSet | None:
        if self._prior_context is not None:
            return self._prior_context.clients
        return self._own_clients

    def _open_event_participant(self) -> None:
        from base.agents.impersonation.status import is_log_native

        source_key = participant_source_key(
            log_native=is_log_native(self._lease()),
            pid=process_metadata()["pid"],
            suffix=uuid4().hex,
        )
        if source_key is None:
            return
        db = self._database()
        if open_local_participant(db, self.lease_id, agent_id=self.agent_id, source_key=source_key):
            participant = LocalParticipant(
                lease_id=self.lease_id,
                agent_id=self.agent_id,
                session_id=self.session_id,
                source_key=source_key,
                db=db,
            )
            self._event_gate = LocalCaptureGate(participant)

    def _seal_wait_seconds(self) -> float:
        boot = self._boot
        match boot:
            case OwnedBoot():
                config = boot.config
                return config.view.general.impersonation_event_seal_wait_seconds
            case BorrowedBoot():
                if self._seal_authority is None:
                    raise RuntimeError(
                        "the attachment holds no configuration owner for its seal wait"
                    )
                raw_wait: Any = self._seal_authority.service_field_value(
                    "impersonation_event_seal_wait_seconds"
                )
                return float(raw_wait)
            case _:
                assert_never(boot)

    def _seal_event_participant(self) -> None:
        if self._event_gate is None:
            return
        drained = close_local_participant_admission(
            self._event_gate, timeout=self._seal_wait_seconds()
        )
        if drained:
            seal_local_participant(self._event_gate)

    def _begin_event_participant_close(self) -> None:
        if self._event_gate is None:
            with self._call_lock:
                self._closing = True
            return
        begin_local_participant_close(
            self._event_gate,
            lambda: setattr(self, "_closing", True),
        )

    def _restore_prior_context(self) -> None:
        if self._bound and self._prior_context is not None:
            process_context.bind_context(self._prior_context)
        elif self._bound:
            process_context.unbind_context()

    def _unbind_exec_turn(self) -> None:
        process_context.unbind_exec_turn()


def _bound_identity(bound: MeivaContext | None) -> BoundIdentity | None:
    identity = None if bound is None else bound.identity
    if identity is None:
        return None
    return BoundIdentity(
        has_lease=identity.lease is not None,
        agent_id=identity.agent_id,
        owns_loop=identity.owns_loop,
    )


def attach(session_id: int | str, *, agent_id: int | None = None) -> Attachment:
    target = lease_target(session_id, agent_id)
    bound = process_context.ambient_context()
    refuse_unless_attachable(
        ProcessFacts(
            is_host=process_context.is_host_process(),
            in_exec_turn=process_context.in_exec_turn(),
            bound=_bound_identity(bound),
        ),
        subject="an external controller",
    )
    boot = (
        borrow_external_process(bound) if bound is not None else own_external_process(ConfigBoot())
    )
    try:
        return Attachment(AttachInputs(target, bound, boot))
    except BaseException:
        if isinstance(boot, OwnedBoot):
            boot.clients.close()
        raise
