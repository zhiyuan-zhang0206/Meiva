from collections.abc import Generator
from contextlib import contextmanager
from typing import Protocol

from base.api_contracts.agent_contract import SdkCaptureAdmission


# meiva:note
# The exclusive attachment owning SDK call admission.
class SdkCaptureOwner(Protocol):
    def admit_sdk_call(self) -> SdkCaptureAdmission | None: ...


# meiva:note
# Retain and release the original call admission supplied by its SDK owner.
@contextmanager
def admitted_local_sdk_call(
    admission: SdkCaptureAdmission | None = None,
) -> Generator[SdkCaptureAdmission | None, None, None]:
    primary: BaseException | None = None
    try:
        yield admission
    except BaseException as exc:
        primary = exc
        raise
    finally:
        if admission is not None:
            try:
                admission.release()
            except BaseException as secondary:
                if primary is None:
                    raise
                primary.add_note(f"SDK capture admission release also failed: {secondary!r}")
