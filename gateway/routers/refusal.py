# meiva:note
# A request a router refuses, as a value: the status and the detail the caller sees.
#
# A pure decision module returns this instead of raising `HTTPException`, so its tests assert
# the refusal without a request; the shell raises it.

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["HttpFailure"]


# meiva:note
# A refusal: the HTTP status and the detail text.
@dataclass(frozen=True)
class HttpFailure:
    status_code: int
    detail: str
