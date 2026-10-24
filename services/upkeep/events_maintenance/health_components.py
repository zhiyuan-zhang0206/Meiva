# meiva:note
# How one resident loop reads on the health endpoint, decided from the facts of its progress tracker.
#
# A loop is degraded when a pass overran its hard deadline (wedged) or when no pass has completed for
# longer than the loop's own timeout (stale). Everything else is ok. `component_facts` returns the
# facts the endpoint renders; the shell formats them into the shared health envelope.
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

Status = Literal["ok", "degraded"]
Progress = Literal["idle", "wedged"]


@dataclass(frozen=True)
class LoopFacts:
    name: str
    timeout_s: float
    stale_for_s: float
    wedged: bool
    last_success_at: str | None
    last_error: str | None


@dataclass(frozen=True)
class ComponentFacts:
    name: str
    status: Status
    last_success: float | None
    last_error: str | None
    progress: Progress
    detail: str | None


def component_facts(loop: LoopFacts) -> ComponentFacts:
    stale = loop.stale_for_s > loop.timeout_s
    if loop.wedged:
        detail = f"wedged: {loop.last_error or 'pass exceeded its deadline'}"
    elif stale:
        detail = f"no progress for {loop.stale_for_s:.0f}s"
    else:
        detail = None
    return ComponentFacts(
        name=loop.name,
        status="degraded" if loop.wedged or stale else "ok",
        last_success=(
            datetime.fromisoformat(loop.last_success_at).timestamp()
            if loop.last_success_at is not None
            else None
        ),
        last_error=loop.last_error,
        progress="wedged" if loop.wedged else "idle",
        detail=detail,
    )
