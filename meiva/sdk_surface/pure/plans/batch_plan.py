# meiva:note
# The contract of the `max_concurrent` knob every SDK batch API shares (`meiva.web.search`,
# `meiva.web.fetch`, `meiva.understand`): its default ceiling and the check that fails before any
# work is scheduled. The executor that honours it (`meiva/sdk_surface/batch.py`) only schedules.
# meiva:link meiva/docs/sdk-surface/sdk-surface.meiva.okf.md

from __future__ import annotations

# meiva:note
# The SDK's convenience batches must not turn one agent action into an account-wide request
# burst. Callers can opt into a different positive ceiling with `max_concurrent`; this default
# bounds the omitted-argument path. An SDK-level constant by design, not cluster config (task
# #3696 exception inventory).
DEFAULT_BATCH_MAX_CONCURRENT = 12


# meiva:note
# Validate the `max_concurrent` knob shared by every SDK batch API, before any work is scheduled.
# `example` names the calling API in the error messages (each entry point keeps its own wording).
# A non-int (a bool included: it is an int subclass, but `True` as a concurrency cap is a bug)
# raises TypeError; a value below 1 raises ValueError, because 0 or a negative cap would block
# every call forever.
def validate_max_concurrent(max_concurrent: int | None, *, example: str) -> None:
    if max_concurrent is not None:
        if not isinstance(max_concurrent, int) or isinstance(max_concurrent, bool):
            raise TypeError(
                f"max_concurrent must be an int or None, got {type(max_concurrent).__name__}. "
                f"Example: {example}"
            )
        if max_concurrent < 1:
            raise ValueError(
                f"max_concurrent must be at least 1, got {max_concurrent} — "
                "0 or negative would block every call forever"
            )
