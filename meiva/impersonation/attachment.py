# meiva:note
# A takeover attachment. What is lease-specific lives here — the derived lease id,
# the active-lease recheck on every call, the delta column's optimistic version and
# the durable event participant the takeover manifest owns. The kernel
# (`base.agents.context.external_attachment`) owns the sequence and the failure handling,
# so a second borrowed identity cannot drift from this one's ordering.
# meiva:link meiva/impersonation/docs/attachment-contracts.meiva.okf.md

from __future__ import annotations

import dataclasses
import importlib
from collections.abc import Mapping
from typing import Any, Self, cast

from base.agents import impersonation as control
from base.agents.context import MeivaContext
from base.agents.context.clients import DatabaseFactory
from base.agents.context.external_attachment import ExternalAttachment
from base.agents.context.identity import AgentIdentity, ExternalLease
from base.clock import SYSTEM_TIMERS, Clock
from base.clock.configuration import clock_config_from_boot
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine import MachineIdentity
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot
from base.config.agent_pins import resolve_agent_config_pins
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.host.env.agent_slices import PluginConfigView
from base.native_process.code_version import CodeVersion
from base.native_process.ownership import process_metadata
from base.packages.plugins.config_view import resolve_agent_plugin_pins
from meiva.boot import initialize_context, process_clients
from meiva.sdk_surface import process_context

__all__ = ["Attachment", "attach"]


def _refuse_unless_attachable() -> MeivaContext | None:
    if process_context.is_host_process():
        raise RuntimeError("a native agent runtime cannot attach an external controller")
    bound = process_context.ambient_context()
    identity = None if bound is None else bound.identity
    if identity is not None and identity.lease is not None:
        raise RuntimeError("this process already has an external attachment")
    if identity is not None and identity.agent_id is not None and identity.owns_loop:
        raise RuntimeError("a native agent runtime cannot attach an external controller")
    return bound


def _attachment_database(
    bound: MeivaContext | None, *, config: ConfigBoot | None, machine: MachineIdentity
) -> DatabaseFactory:
    if bound is not None:
        return bound.clients.database
    if config is None:
        raise RuntimeError("an independent attachment needs its configuration owner")
    version = CodeVersion(process_context.loaded_code_image())
    gate = ProcessDbGate(version=version.get, process="unknown")
    return lambda: Database(
        db_config_from_boot(config),
        gate=gate,
        direct_url=ClusterDirectUrl(machine.host),
    )


def _agent_codec() -> Any:
    return importlib.import_module("agent.extensions.plugin_state_codec")


class _Codec:
    def __init__(self, attachment: Attachment) -> None:
        self._attachment = attachment

    def load_snapshot(self, agent_id: int) -> Any:
        return _agent_codec().load_snapshot(agent_id, database=self._attachment._database())

    def plugin_state_schema(self, state: Any) -> Any:
        del state
        return _agent_codec().plugin_state_schema

    def encode_plugin_delta(
        self, update: Mapping[str, Any], state_cls: type[Any]
    ) -> dict[str, str]:
        return cast("dict[str, str]", _agent_codec().encode_plugin_delta(update, state_cls))

    def decode_plugin_delta(self, encoded: dict[str, str], state_cls: type[Any]) -> Any:
        return _agent_codec().decode_plugin_delta(encoded, state_cls)

    def apply_plugin_delta(self, state: Any, delta: Any) -> None:
        _agent_codec().apply_plugin_delta(state, delta)


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
        control.merge_plugin_delta(
            attachment._database(),
            attachment.lease_id,
            encoded,
            expected_version=expected_version,
            local_machine=attachment._machine.name(),
        )
        attachment._version += 1
        return attachment._version


class Attachment(ExternalAttachment):
    def __init__(
        self, lease_id: str, *, agent_id: int | None = None, session_id: int | None = None
    ) -> None:
        if session_id is not None and agent_id is None:
            raise ValueError("an integer session requires an agent_id")
        bound = _refuse_unless_attachable()
        self.lease_id = lease_id
        self._lease_agent_id = agent_id
        self._lease_session_id = session_id
        self.config: tuple[Mapping[str, Any], PluginConfigView] | None = None
        self._machine: MachineIdentity
        self._database: DatabaseFactory
        self._clock_factory: Any = None
        self._event_seal_wait_reader: Any = None
        self.agent_id: int = 0
        self.session_id: int = 0
        self._version: int = 0
        if process_context.in_exec_turn():
            raise RuntimeError("an exec turn cannot attach an external controller")
        super().__init__(identity=_LeaseIdentity(self), codec=_Codec(self))
        self.open(bound)

    def _prepare(self) -> None:
        bound = self._prior_context
        config = None if bound is not None else ConfigBoot()
        if config is not None:
            config.read_process_environment()
        self._boot_config = config
        self._clock_factory = (
            bound.require_clock
            if bound is not None
            else (
                lambda: Clock(
                    clock_config_from_boot(cast(ConfigBoot, config)), timers=SYSTEM_TIMERS
                )
            )
        )
        self._machine = (
            bound.require_machine()
            if bound is not None
            else machine_from_boot(cast(ConfigBoot, config))
        )
        self._database = _attachment_database(bound, config=config, machine=self._machine)
        if self._lease_session_id is not None:
            from base.agents.impersonation.sessions import private_id

            self.lease_id = private_id(
                self._database(),
                cast(int, self._lease_agent_id),
                self._lease_session_id,
                local_machine=self._machine.name(),
            )
        if bound is None:
            self._own_clients = process_clients(
                database=self._database, config=config, machine=self._machine
            )
        lease = self._lease()
        self.agent_id = int(lease["agent_id"])
        self.session_id = int(lease["session_id"])
        self._version = int(lease["delta_version"])
        self._bind_borrowed_context()
        initialize_context(
            process_context.current_context(),
            surface=False,
            config=config,
            clock_factory=self._clock_factory,
            validate_state=_agent_codec().plugin_state_schema,
        )
        if config is None:
            from meiva.sdk_surface.settings import config_authority

            authority = config_authority()
            self._event_seal_wait_reader = lambda: cast(
                float, authority.service_field_value("impersonation_event_seal_wait_seconds")
            )
        else:
            self._event_seal_wait_reader = lambda: (
                config.view.general.impersonation_event_seal_wait_seconds
            )

    def _applied_version(self, state: Any, lease: Mapping[str, Any]) -> int:
        receipt = state.impersonation_applied
        checkpoint_version = receipt["version"] if receipt.get("lease_id") == self.lease_id else 0
        return max(lease["applied_version"], checkpoint_version)

    def _resolve_config(
        self, overlay: Mapping[str, Any] | None, birth: Mapping[str, Any] | None
    ) -> None:
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

    def capture_local_event(self, event: Any) -> Any:
        from base.agents.impersonation.manifest import capture_local_event

        return capture_local_event(event, gate=self._event_gate)

    def _lease(self) -> dict[str, Any]:
        lease = control.require_active(
            self._database(), self.lease_id, local_machine=self._machine.name()
        )
        if lease["machine"] != self._machine.name():
            raise RuntimeError(f"external SDK must run on agent machine {lease['machine']!r}")
        return lease

    def _validate(self) -> int:
        if self._closed:
            raise RuntimeError("external attachment is closed")
        lease = self._lease()
        if lease["delta_version"] != self._version:
            raise RuntimeError(
                "another attachment changed plugin state; attach again before acting"
            )
        return self.agent_id

    def _telemetry_clients(self) -> Any:
        if self._prior_context is not None:
            return self._prior_context.clients
        return self._own_clients

    def _open_event_participant(self) -> None:
        from uuid import uuid4

        from base.agents.impersonation.manifest import (
            LocalCaptureGate,
            LocalParticipant,
            is_log_native,
            open_local_participant,
        )

        if not is_log_native(self._lease()):
            return
        source_key = f"attachment:{process_metadata()['pid']}:{uuid4().hex}"
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

    def _seal_event_participant(self) -> None:
        if self._event_gate is None:
            return
        from base.agents.impersonation.manifest import (
            close_local_participant_admission,
            seal_local_participant,
        )

        drained = close_local_participant_admission(
            self._event_gate,
            timeout=self._event_seal_wait_reader(),
        )
        if drained:
            seal_local_participant(self._event_gate)

    def _begin_event_participant_close(self) -> None:
        if self._event_gate is None:
            with self._call_lock:
                self._closing = True
            return
        from base.agents.impersonation.manifest import begin_local_participant_close

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

    def __enter__(self) -> Self:
        return super().__enter__()


def attach(session_id: int | str, *, agent_id: int | None = None) -> Attachment:
    if isinstance(session_id, int) and not isinstance(session_id, bool) and agent_id is not None:
        return Attachment("", agent_id=agent_id, session_id=session_id)
    if isinstance(session_id, str) and agent_id is None:
        return Attachment(session_id)
    raise ValueError("attach requires an agent_id and an integer session_id")
