# meiva:note
# Which results the exec child delivers telemetry for, and which of them a failed delivery
# rewrites. A delivery that comes back unfinished is a warning and never changes the envelope.
# A delivery that raises turns a success into a crash envelope the parent can see, but never
# replaces a crash the child already reported: that crash stays primary. Every `ResultKind` has a
# row, and a kind outside them is a protocol violation that raises instead of reading as "no".
# meiva:link agent/execution/docs/child-boundaries.meiva.okf.md

from __future__ import annotations

from dataclasses import dataclass

from agent.execution.result_kind import ResultKind


@dataclass(frozen=True)
class _Delivery:
    delivers: bool
    rewrites_on_failure: bool


_DELIVERY: dict[ResultKind, _Delivery] = {
    "done": _Delivery(delivers=True, rewrites_on_failure=True),
    "lifecycle": _Delivery(delivers=True, rewrites_on_failure=True),
    "crashed": _Delivery(delivers=True, rewrites_on_failure=False),
    "cancelled": _Delivery(delivers=False, rewrites_on_failure=False),
    "timed_out": _Delivery(delivers=False, rewrites_on_failure=False),
}


def _delivery(kind: ResultKind) -> _Delivery:
    try:
        return _DELIVERY[kind]
    except KeyError:
        raise ValueError(f"unknown result kind {kind!r}") from None


def delivers_telemetry(kind: ResultKind) -> bool:
    return _delivery(kind).delivers


def rewrites_result_after_delivery_failure(kind: ResultKind) -> bool:
    return _delivery(kind).rewrites_on_failure


# meiva:note
# What the child does once its owned and ordinary telemetry drains have returned. A drain that
# came back unfinished is a warning and nothing more: it never stops the owned exporter from
# being finalized and never touches the result envelope (only a drain that raises can, see
# `rewrites_result_after_delivery_failure`). `owned_unfinished` is `None` when the child owns no
# pipeline, which leaves nothing to finalize.
@dataclass(frozen=True)
class FinalizePlan:
    warn_owned_unfinished: bool
    warn_ordinary_unfinished: bool
    finalize_owned: bool


def plan_finalize(*, owned_unfinished: bool | None, ordinary_unfinished: bool) -> FinalizePlan:
    return FinalizePlan(
        warn_owned_unfinished=owned_unfinished is True,
        warn_ordinary_unfinished=ordinary_unfinished,
        finalize_owned=owned_unfinished is not None,
    )
