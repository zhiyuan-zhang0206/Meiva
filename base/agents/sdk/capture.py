"""Explicit SDK attachment capture contracts, without importing receipt storage."""

from typing import Any, Protocol

from base.api_contracts.agent_contract import SdkCaptureAdmission


class SdkCaptureOwner(Protocol):
    """The exclusive attachment owning admission and direct local audit capture."""

    # meiva:note
    # `fn` is the call's dotted SDK entry (`files.write`), the same key the SDK usage
    # meter reports; a gate that scopes what the caller may do needs it.
    def admit_sdk_call(self, fn: str) -> SdkCaptureAdmission | None:
        """Reject calls after close starts; otherwise retain their receipt admission."""
        ...

    def capture_local_event(self, event: Any) -> Any:
        """Capture a direct local audit with this attachment's original gate."""
        ...
