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
# Only a fresh id is assigned to the request, so every response of one request agrees. A caller
# that cannot draw one passes `fresh=None`: a request with neither an active trace nor an
# assigned id is then a wiring defect, because the middleware that binds the id never ran.
def correlation(*, active: str | None, assigned: object, fresh: str | None) -> Correlation:
    if active is not None:
        return Correlation(active, assign=False)
    if isinstance(assigned, str):
        return Correlation(assigned, assign=False)
    if fresh is None:
        raise ValueError("the request carries no correlation id and none was drawn for it")
    return Correlation(fresh, assign=True)
