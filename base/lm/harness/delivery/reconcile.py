from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum


# meiva:note
# What a harness's own durable record says about one input, asked after a crash or a restart when
# the live acknowledgements are gone. IN_CONTEXT: the model has it (or will, with no further
# delivery). PERSISTED_UNDELIVERED: the harness stored it and will deliver it by itself, so it must
# not be delivered again. NOT_FOUND: the record has no trace of it, so it is safe to re-deliver.
# No harness de-duplicates, which is why only NOT_FOUND allows a re-delivery. UNKNOWN: the record
# cannot tell for this input (Antigravity records only inputs sent through its message queue, so an
# input that started a turn on stdin leaves no trace there whether or not it ran); it is never
# re-delivered, the caller consumes it with the uncertainty marker.
# meiva:link base/lm/harness/docs/delivery.md
class Reconciliation(StrEnum):
    IN_CONTEXT = "in_context"
    PERSISTED_UNDELIVERED = "persisted_undelivered"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"


# meiva:note
# The harness's durable record could not be asked: the session has no record on this machine, or the
# file is missing. It is not "not found": an input is judged absent only by a record that exists and
# does not hold it, so the caller falls back to the marker instead of guessing.
# meiva:link base/lm/harness/docs/delivery.md
class ReconciliationUnavailableError(RuntimeError):
    pass


# meiva:note
# The home directory a harness keeps its records under, named by the session's own environment: the
# one place the answer comes from, so a test or another machine hands in a different one. An
# environment without it cannot say where the record is.
def home_in(environ: Mapping[str, str]) -> str:
    home = environ.get("HOME")
    if not home:
        raise ReconciliationUnavailableError(
            "the session environment names no HOME to find the harness's record under"
        )
    return home
