# meiva:note
# What a native LGTM archive download says to the operator: the heartbeat, the failure of each
# attempt and the final error, with the time used, the budget left and the bytes received.

from __future__ import annotations

from cli.commands.observability.plans.lgtm.download_retry import AttemptState, DownloadLimits

__all__ = [
    "attempt_failure_line",
    "budget_exceeded_message",
    "done_line",
    "give_up_message",
    "progress_line",
]


# meiva:note
# The heartbeat line while a body streams in.
def progress_line(*, received: int, total: int | None, elapsed_s: float) -> str:
    percent = f" ({received * 100 // total}%)" if total else ""
    return f"  · lgtm native: {received / 1e6:.1f} MB{percent} in {elapsed_s:.0f}s"


# meiva:note
# The line that ends a successful download.
def done_line(*, received: int, elapsed_s: float) -> str:
    return f"  · lgtm native: downloaded {received / 1e6:.1f} MB in {elapsed_s:.0f}s"


# meiva:note
# Why a read or an open was refused: the deadline passed, with the bytes that arrived by then.
def budget_exceeded_message(limits: DownloadLimits, *, received: int, remaining_s: float) -> str:
    return (
        f"lgtm native download exceeded its {limits.total_budget_s:.0f}s budget "
        f"({received} bytes received, "
        f"{max(-remaining_s, 0.0):.1f}s past the deadline)"
    )


# meiva:note
# The line one failed attempt prints, with the time used, the budget left and the bytes received.
def attempt_failure_line(limits: DownloadLimits, state: AttemptState, *, error: str) -> str:
    return (
        f"  ! lgtm native: download attempt {state.attempt}/{limits.attempts} failed after "
        f"{state.used_s:.0f}s ({max(state.remaining_s, 0.0):.0f}s of the "
        f"{limits.total_budget_s:.0f}s budget left, {state.received} bytes "
        f"received): {error}"
    )


# meiva:note
# The error the last failed attempt raises once the attempts or the budget are spent.
def give_up_message(limits: DownloadLimits, state: AttemptState, *, url: str, error: str) -> str:
    return (
        f"failed to download native LGTM backend from {url} after {state.attempt} attempt(s) "
        f"({state.used_s:.0f}s used of the {limits.total_budget_s:.0f}s budget, "
        f"{max(state.remaining_s, 0.0):.0f}s left, {state.received} bytes received by the "
        f"last attempt): {error}"
    )
