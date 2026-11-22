from __future__ import annotations

from base.host.permissions_helper.client import UserAbortError

from .plan import CORNER_FAILSAFE_CODE


class CornerFailsafeError(UserAbortError):
    def __init__(self, message: str, *, code: str = CORNER_FAILSAFE_CODE) -> None:
        self.code = code
        super().__init__(f"{code}: {message}" if code not in message else message)
