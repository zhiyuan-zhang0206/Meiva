"""Explicit SDK attachment capture contracts, without importing receipt storage."""

from typing import Protocol

from base.api_contracts.agent_contract import SdkCaptureAdmission


# meiva:note
# The exclusive attachment owning SDK call admission.
class SdkCaptureOwner(Protocol):
    def admit_sdk_call(self) -> SdkCaptureAdmission | None:
        """Reject calls after close starts; otherwise retain their receipt admission."""
        ...
