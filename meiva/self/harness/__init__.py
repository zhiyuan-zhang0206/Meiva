from __future__ import annotations

import os

from base.log import guard_default_logging
from meiva import boot
from meiva.sdk_surface import process_context, sdk_disable

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
    guard_default_logging()
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
