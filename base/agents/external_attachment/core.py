# meiva:note
# An attachment borrows a validated external identity and restores the prior
# process context on detach. Its own client set and admission gate are distinct
# from the SDK installation's longer-lived cold producer. Each admitted SDK call
# retains its original durable capture gate even when another attachment becomes
# current. Closing admission and marking the attachment closing share one lock:
# already-admitted calls may drain, later calls are refused. Close attempts
# plugin-state flush, receipt closure, telemetry delivery and detach; a later
# cleanup error cannot replace the primary failure.
#
# What differs per identity — a takeover lease or a harness registration — is the
# identity itself, its codec and three hooks. The sequence below (boot, replay,
# validate, open, admit, flush, close, detach) exists once so a second identity
# cannot drift from the first one's failure handling.
# meiva:link base/agents/external_attachment/docs/external-attachment.meiva.okf.md

from __future__ import annotations

import sys
from collections.abc import Mapping
from contextlib import ExitStack
from threading import Lock, local
from types import TracebackType
from typing import Any, Self

from base.agents.external_attachment.identity import AttachmentCodec, IdentitySource
from base.log import logger

_attachment_lock = Lock()
_close_flush_permission = local()


def _close_flush_permitted() -> bool:
    return bool(getattr(_close_flush_permission, "allowed", False))


def _deliver_telemetry_before_detach(*, clients: Any) -> None:
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


class ExternalAttachment:
    """One borrowed identity in one local Python process."""

    def __init__(self, *, identity: IdentitySource, codec: AttachmentCodec) -> None:
        self.identity = identity
        self._codec = codec
        self._closed = False
        self._closing = False
        self._stack = ExitStack()
        self._event_gate: Any = None
        self._call_lock = Lock()
        self._prior_context: Any = None
        self._bound = False
        self._own_clients: Any = None
        self._state_cls: type[Any]
        self._state: Any = None

    def open(self, prior_context: Any) -> None:
        """Bind the identity; the subclass calls this once its own fields are set."""

        self._prior_context = prior_context
        if not _attachment_lock.acquire(blocking=False):
            raise RuntimeError("this process already has an external attachment")
        try:
            self._initialize()
        except BaseException as primary:
            try:
                self._detach()
            except BaseException as secondary:
                primary.add_note(f"Attachment constructor cleanup also failed: {secondary!r}")
            raise

    def _initialize(self) -> None:
        self._prepare()
        state, overlay, birth = self._codec.load_snapshot(self.identity.agent_id())
        self._state_cls = type(state)
        self._resolve_config(overlay, birth)
        lease = self.identity.require()
        applied = self._applied_version(state, lease)
        for encoded in lease["plugin_delta"][applied:]:
            self._codec.apply_plugin_delta(
                state, self._codec.decode_plugin_delta(encoded, self._state_cls)
            )
        self._validate()
        self._open_event_participant()
        self._state = state
        self._set_state(state)

    def _prepare(self) -> None:
        """Hook: boot config, resolve the identity, bind the borrowed context."""

    def _resolve_config(
        self, overlay: Mapping[str, Any] | None, birth: Mapping[str, Any] | None
    ) -> None:
        raise NotImplementedError

    def _applied_version(self, state: Any, lease: Mapping[str, Any]) -> int:
        return int(lease["applied_version"])

    def _set_state(self, state: Any) -> None:
        raise NotImplementedError

    def _state_update(self) -> Any:
        raise NotImplementedError

    def _clear_state_update(self) -> None:
        raise NotImplementedError

    def _open_event_participant(self) -> None:
        """Hook: open the durable event participant, when the identity has one."""

    def _seal_event_participant(self) -> None:
        """Hook: seal the participant once its admission has drained."""

    def _begin_event_participant_close(self) -> None:
        with self._call_lock:
            self._closing = True

    def native_snapshot(self) -> Any:
        self._validate()
        snapshot, _overlay, _birth = self._codec.load_snapshot(self.identity.agent_id())
        return snapshot

    def admit_sdk_call(self) -> Any:
        with self._call_lock:
            if self._closed or self._closing:
                raise RuntimeError("external attachment is closing or closed")
            gate = self._event_gate
            if gate is None:
                self._validate()
                return None
            with gate.condition:
                if self._closed or self._closing or gate.admission_closed:
                    raise RuntimeError("external attachment is closing or closed")
                self._validate()
                return gate.admit()

    def capture_local_event(self, event: Any) -> Any:
        return event

    def _validate(self) -> int:
        return self.identity.validate()

    def flush(self) -> None:
        self._flush(allow_closing=_close_flush_permitted())

    def _flush(self, *, allow_closing: bool) -> None:
        if self._closing and not allow_closing:
            raise RuntimeError("external attachment is closing")
        self._validate()
        update = self._state_update()
        if not isinstance(update, dict):
            raise TypeError("external plugin state update must be a dict")
        if update:
            encoded = self._codec.encode_plugin_delta(update, self._state_cls)
            self.identity.merge_delta(encoded, expected_version=self.identity.delta_version())
            self._clear_state_update()

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
        raise NotImplementedError

    def _detach(self) -> None:
        if self._closed:
            return
        self._closed = True
        primary: BaseException | None = None
        try:
            for cleanup in (
                self._restore_prior_context,
                self._unbind_exec_turn,
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
            try:
                _attachment_lock.release()
            except RuntimeError:
                pass
        if primary is not None:
            raise primary

    def _restore_prior_context(self) -> None:
        raise NotImplementedError

    def _unbind_exec_turn(self) -> None:
        """Hook: drop the exec-turn binding, when the surface has one."""

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
