# meiva:note
# Which correlation id a gateway response carries: values in, the id out.

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Correlation", "correlation"]


# meiva:note
# The id an error envelope reports, and whether the request must remember it.
@dataclass(frozen=True)
class Correlation:
    trace_id: str
    assign: bool


# meiva:note
# The active OTel trace id wins; else the id this request already carries; else `fresh`.
#
# Only a fresh id is assigned to the request, so every response of one request agrees.
def correlation(*, active: str | None, assigned: object, fresh: str) -> Correlation:
    if active is not None:
        return Correlation(active, assign=False)
    if isinstance(assigned, str):
        return Correlation(assigned, assign=False)
    return Correlation(fresh, assign=True)
