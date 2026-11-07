from __future__ import annotations

import os

from meiva import boot
from meiva.sdk_surface import process_context, sdk_disable

__all_for_meiva__ = ["attach"]

HANDLE_ENV = "MEIVA_HARNESS_HANDLE"


def __getattr__(name: str) -> object:
    """Serve the kernel's names on first use, so `import meiva` stays light."""
    match name:
        case "HarnessRegistration":
            from .registration import HarnessRegistration

            return HarnessRegistration
        case "RegistrationError":
            from .registration import RegistrationError

            return RegistrationError
        case "handle_digest":
            from .registration import handle_digest

            return handle_digest
        case "FileRegistrationStore":
            from .store import FileRegistrationStore

            return FileRegistrationStore
        case "registration_store":
            from .store import registration_store

            return registration_store
        case "HarnessAttachment":
            from .attachment import HarnessAttachment

            return HarnessAttachment
        case "open_attachment":
            from .attachment import open_attachment

            return open_attachment
        case "resolve_registration":
            from .attachment import resolve_registration

            return resolve_registration
        case _:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def ensure_identity() -> object:
    """Attach this process's harness identity the first time one is needed.

    Returns the attachment, or None when this process has no handle to use, already
    has an identity of its own, or the session could not be attached — the last case
    raises `boot.HarnessAttachError` and records it, so a later read reports the same
    reason instead of trying the registration again.
    """
    if process_context.is_host_process():
        return None
    bound = process_context.bound_context()
    if bound is not None:
        return bound.sdk_capture
    handle = os.environ.get(HANDLE_ENV)
    if not handle:
        return None
    sdk_disable.raise_load_failure()
    from meiva.self.harness.attachment import open_attachment

    try:
        attachment = open_attachment(handle)
    except Exception as exc:
        if isinstance(exc, boot.HarnessAttachError):
            raise
        boot.record_harness_failure(exc)
        raise boot.harness_failure(exc) from exc
    import atexit

    atexit.register(attachment.close)
    return attachment


def attach(handle: str | None = None) -> object:
    """Bind this external process to the Meiva agent identity it was started as.

    Call it with no argument: the handle comes from `MEIVA_HARNESS_HANDLE`, which the
    bridge exported into this process. The call is idempotent — the first one
    attaches, later ones return the same attachment — and every session's first
    identity read does the same thing, so calling it explicitly is optional.

    Raises:
        RegistrationError: no handle in this process, or no session matches it.
        HarnessAttachError: the session could not be attached (expired, ended,
            another machine); the message carries the reason and says not to retry.
        RuntimeError: this process already has an identity of its own.
    """
    if process_context.is_host_process():
        raise RuntimeError(
            "meiva.attach found no identity to borrow: this process runs the shared host"
        )
    if handle is not None and handle != os.environ.get(HANDLE_ENV):
        raise _registration_error(
            "meiva.attach binds the session this process was started as; it cannot take "
            "another session's handle. Use meiva.self.harness.open_attachment for that."
        )
    attachment = ensure_identity()
    if attachment is not None:
        return attachment
    raise no_identity_error(os.environ.get(HANDLE_ENV))


# meiva:note
# Which failure a call without an identity reports: a process the bridge did not start has no
# handle at all -- the variable is exported per launch or not exported -- so an empty value is the
# same answer as a missing one, and only a handle that is there makes this the borrow failure.
def no_identity_error(handle: str | None) -> BaseException:
    if not handle:
        return _registration_error(
            "meiva.attach needs the session handle: run inside a process the bridge "
            f"started, which exports {HANDLE_ENV}"
        )
    return RuntimeError(
        "meiva.attach found no identity to borrow: this process already has one, or it "
        "runs the shared host"
    )


def _registration_error(message: str) -> BaseException:
    from meiva.self.harness.registration import RegistrationError

    return RegistrationError(message)
