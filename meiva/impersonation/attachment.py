# meiva:note
# An attachment borrows a validated controller identity and restores the prior process
# context on detach. Its own client set and admission gate are distinct from the SDK
# installation's longer-lived cold producer. Each admitted SDK call retains its original
# durable capture gate even when another attachment becomes current. Closing admission
# and marking the attachment closing share one lock: already-admitted calls may drain,
# later calls are refused. Close attempts plugin-state flush, receipt closure, telemetry
# delivery and detach; a later cleanup error cannot replace the primary failure. A
# still-running admitted call retains its original receipt and seals it only when it
# actually drains; a finite close wait cannot certify that source empty.
# meiva:link meiva/impersonation/docs/attachment-contracts.meiva.okf.md

from __future__ import annotations

import dataclasses
import importlib
import sys
from collections.abc import Mapping
from contextlib import ExitStack
from threading import Lock, local
from types import TracebackType
from typing import Any, Self, cast
from uuid import uuid4

from base.agents import impersonation as control
from base.agents.context import MeivaContext
from base.agents.context.clients import ClientSet, DatabaseFactory
from base.agents.context.identity import AgentIdentity, ExternalLease
from base.agents.impersonation.capabilities import (
    Capability,
    CapabilityDenied,
    declared_capabilities,
)
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
from base.log import logger
from base.native_process.code_version import CodeVersion
from base.native_process.ownership import process_metadata
from base.packages.plugins.config_view import resolve_agent_plugin_pins
from meiva.boot import initialize_context, process_clients
from meiva.impersonation.capabilities import primary_capability, required_capabilities
from meiva.sdk_surface import process_context

# meiva:note
# Enforcement of a lease's declared capabilities
# (base/agents/impersonation/capabilities.py). `warn` is the shipped default: an
# unclassified entry must not stop a takeover that worked yesterday, so the first
# release records what it would have refused and changes no behaviour. Turning
# this to `enforce` is an operational decision, not a code change.
CAPABILITY_MODES = ("off", "warn", "enforce")

_attachment_lock = Lock()
_close_flush_permission = local()


def _close_flush_permitted() -> bool:
    return bool(getattr(_close_flush_permission, "allowed", False))


def _deliver_telemetry_before_detach(*, clients: ClientSet) -> None:
    if "base.telemetry" not in sys.modules:
        return
    from base import telemetry

    owned = clients.sync_events()
    if owned.status is telemetry.DrainStatus.UNFINISHED:
        logger.warning(
            "external attachment: owned telemetry delivery is unfinished; "
            "queued records may be lost or land later"
        )
    try:
        result = telemetry.sync()
        if result.status is telemetry.DrainStatus.UNFINISHED:
            logger.warning(
                "external attachment: ordinary telemetry delivery is unfinished; "
                "queued records may be lost or land later"
            )
        clients.finalize_events()
    except Exception:
        logger.opt(exception=True).warning(
            "external attachment: telemetry delivery before detach failed; "
            "ordinary queued records may be lost or land later"
        )


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


class Attachment:
    def __init__(
        self, lease_id: str, *, agent_id: int | None = None, session_id: int | None = None
    ) -> None:

        bound = _refuse_unless_attachable()
        if session_id is not None and agent_id is None:
            raise ValueError("an integer session requires an agent_id")
        self.lease_id = lease_id
        self._closed = False
        self._closing = False
        self._stack = ExitStack()

        self.config: tuple[Mapping[str, Any], PluginConfigView] | None = None
        self._event_gate: Any = None
        self._call_lock = Lock()

        self._prior_context = bound
        self._bound = False
        self._own_clients: ClientSet | None = None

        self._state_cls: type[Any]
        if process_context.in_exec_turn():
            raise RuntimeError("an exec turn cannot attach an external controller")
        if not _attachment_lock.acquire(blocking=False):
            raise RuntimeError("this process already has an external attachment")
        try:
            self._initialize(bound, agent_id=agent_id, session_id=session_id)
        except BaseException as primary:
            try:
                self._detach()
            except BaseException as secondary:
                primary.add_note(f"Attachment constructor cleanup also failed: {secondary!r}")
            raise

    def _initialize(
        self, bound: MeivaContext | None, *, agent_id: int | None, session_id: int | None
    ) -> None:
        config = None if bound is not None else ConfigBoot()
        if config is not None:
            config.read_process_environment()
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
        if session_id is not None:
            from base.agents.impersonation.sessions import private_id

            self.lease_id = private_id(
                self._database(),
                cast(int, agent_id),
                session_id,
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
            self._capability_mode_reader = lambda: cast(
                str, authority.service_field_value("impersonation_capability_enforcement")
            )
        else:
            self._event_seal_wait_reader = lambda: (
                config.view.general.impersonation_event_seal_wait_seconds
            )
            self._capability_mode_reader = lambda: (
                config.view.general.impersonation_capability_enforcement
            )
        state, overlay, birth = _agent_codec().load_snapshot(
            self.agent_id, database=self._database()
        )
        self._state_cls = type(state)
        self._resolve_config(overlay, birth)

        receipt = state.impersonation_applied
        checkpoint_version = receipt["version"] if receipt.get("lease_id") == self.lease_id else 0
        applied = max(lease["applied_version"], checkpoint_version)
        codec = _agent_codec()
        for encoded in lease["plugin_delta"][applied:]:
            codec.apply_plugin_delta(state, codec.decode_plugin_delta(encoded, self._state_cls))
        self._validate()
        self._open_event_participant()
        process_context.set_state(state)
        process_context.set_state_update({})

    def native_snapshot(self) -> Any:
        self._validate()
        snapshot, _overlay, _birth = _agent_codec().load_snapshot(
            self.agent_id, database=self._database()
        )
        return snapshot

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

    def admit_sdk_call(self, fn: str) -> Any:
        with self._call_lock:
            if self._closed or self._closing:
                raise RuntimeError("external attachment is closing or closed")
            gate = self._event_gate
            if gate is None:
                self._check_capability(fn, self._checked_lease())
                return None
            with gate.condition:
                if self._closed or self._closing or gate.admission_closed:
                    raise RuntimeError("external attachment is closing or closed")
                self._check_capability(fn, self._checked_lease())
                return gate.admit()

    # meiva:note
    # Refuse (or record) an SDK entry this lease does not declare. A lease without
    # a declaration is a pre-vocabulary one and keeps the full surface it was issued
    # with; NULL never means "minimal". Everything else is closed by default: an
    # entry nobody classified is refused, because a silent allowance is how a
    # surface grows past its owner. An entry that needs several capabilities passes
    # only when the lease holds every one of them - partial authority is not authority.
    def _check_capability(self, fn: str, lease: Mapping[str, Any]) -> None:
        declared = declared_capabilities(lease)
        if declared is None:
            return
        required = required_capabilities(fn)
        if required is not None and set(required) <= declared:
            return
        mode = self._capability_mode_reader()
        if mode not in CAPABILITY_MODES:
            raise RuntimeError(f"unknown impersonation_capability_enforcement value: {mode!r}")
        if mode == "off":
            return
        self._record_capability_refusal(fn, required, mode, lease)
        if mode == "enforce":
            raise CapabilityDenied(fn, required, mode)

    # meiva:note
    # One event per refused (or would-be-refused) call. `capability` keeps the entry's
    # primary requirement and stays empty when no capability classifies the entry -
    # that is the finding, not a missing field. `capabilities` carries the whole
    # requirement set so a reader can see every missing piece, not just the first.
    def _record_capability_refusal(
        self,
        fn: str,
        required: tuple[Capability, ...] | None,
        mode: str,
        lease: Mapping[str, Any],
    ) -> None:
        from base import telemetry

        telemetry.emit(
            "telemetry",
            "impersonation_capability_refused",
            level="warning" if mode == "warn" else "error",
            agent_id=self.agent_id,
            attributes={
                "lease_id": str(lease.get("id", self.lease_id)),
                "entry": fn,
                "capability": "" if required is None else primary_capability(required).value,
                "capabilities": [] if required is None else [item.value for item in required],
                "mode": mode,
            },
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

    def _checked_lease(self) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("external attachment is closed")
        lease = self._lease()
        if lease["delta_version"] != self._version:
            raise RuntimeError(
                "another attachment changed plugin state; attach again before acting"
            )
        return lease

    def _validate(self) -> int:
        self._checked_lease()
        return self.agent_id

    def flush(self) -> None:
        self._flush(allow_closing=_close_flush_permitted())

    def _flush(self, *, allow_closing: bool) -> None:

        if self._closing and not allow_closing:
            raise RuntimeError("external attachment is closing")
        self._validate()
        update = process_context.get_state_update()
        if not isinstance(update, dict):
            raise TypeError("external plugin state update must be a dict")
        if update:
            encoded = _agent_codec().encode_plugin_delta(update, self._state_cls)
            control.merge_plugin_delta(
                self._database(),
                self.lease_id,
                encoded,
                expected_version=self._version,
                local_machine=self._machine.name(),
            )
            self._version += 1
            update.clear()

    def close(self) -> None:
        if self._closed:
            self._close_owned_clients()
            return
        if self._closing:
            return
        primary: BaseException | None = None
        try:
            self._begin_event_participant_close()
            was_permitted = _close_flush_permitted()
            _close_flush_permission.allowed = True
            try:
                self.flush()
            finally:
                _close_flush_permission.allowed = was_permitted
        except BaseException as exc:
            primary = exc

        for cleanup in (
            self._seal_event_participant,
            self._deliver_telemetry,
            self._detach,
        ):
            try:
                cleanup()
            except BaseException as secondary:
                if primary is None:
                    primary = secondary
                else:
                    primary.add_note(f"Attachment close cleanup also failed: {secondary!r}")
        if primary is not None:
            raise primary

    def _deliver_telemetry(self) -> None:
        clients = self._own_clients
        if self._prior_context is not None:
            clients = self._prior_context.clients
        if clients is None:
            raise RuntimeError("the attachment has no event producer owner")
        _deliver_telemetry_before_detach(clients=clients)

    def _open_event_participant(self) -> None:
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

    def _detach(self) -> None:

        if self._closed:
            return
        self._closed = True
        primary: BaseException | None = None
        try:
            for cleanup in (
                self._restore_prior_context,
                process_context.unbind_exec_turn,
                self._stack.close,
                self._close_owned_clients,
            ):
                try:
                    cleanup()
                except BaseException as secondary:
                    if primary is None:
                        primary = secondary
                    else:
                        primary.add_note(f"Attachment detach cleanup also failed: {secondary!r}")
        finally:
            _attachment_lock.release()
        if primary is not None:
            raise primary

    def _restore_prior_context(self) -> None:
        if self._bound and self._prior_context is not None:
            process_context.bind_context(self._prior_context)
        elif self._bound:
            process_context.unbind_context()

    def _close_owned_clients(self) -> None:
        if self._own_clients is not None:
            self._own_clients.close()

    def __enter__(self) -> Self:
        try:
            self._validate()
        except BaseException as primary:
            try:
                self._detach()
            except BaseException as secondary:
                primary.add_note(f"Attachment entry cleanup also failed: {secondary!r}")
            raise
        return self

    def __exit__(
        self,
        _kind: type[BaseException] | None,
        _error: BaseException | None,
        _trace: TracebackType | None,
    ) -> None:
        try:
            self.close()
        except BaseException as secondary:
            if _error is None:
                raise
            _error.add_note(f"Attachment context cleanup also failed: {secondary!r}")


def attach(session_id: int | str, *, agent_id: int | None = None) -> Attachment:
    if isinstance(session_id, int) and not isinstance(session_id, bool) and agent_id is not None:
        return Attachment("", agent_id=agent_id, session_id=session_id)
    if isinstance(session_id, str) and agent_id is None:
        return Attachment(session_id)
    raise ValueError("attach requires an agent_id and an integer session_id")
