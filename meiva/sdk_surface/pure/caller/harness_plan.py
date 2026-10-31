# meiva:note
# Which harness an SDK action reports, decided over facts the caller read. A native process that
# carries an SDK capture handle is an attached external controller, so the name it reports is the
# harness its capture registered (or `external` when it recorded none); a non-native harness names
# itself, and a process with no context reports the native harness.
from __future__ import annotations

from base.api_contracts.agent_contract import Harness

__all__ = ["harness_name"]


def harness_name(
    harness: Harness, *, capture_handle: object | None, registration_harness: object | None
) -> str:
    if harness is not Harness.MEIVA:
        return harness.value
    if capture_handle is not None:
        return str(registration_harness or "external")
    return harness.value
