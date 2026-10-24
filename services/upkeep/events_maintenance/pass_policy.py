# meiva:note
# What a resident loop does after one of its passes, decided from how the pass ended.
#
# The shell classifies the exception a pass raised (if any) into a `Failure`; `verdict` says what
# follows. A wedged pass parks the loop for the watchdog. Schema drift cannot heal by retrying, so it
# ends the process. An unreachable database skips the pass without spending the failure budget. Any
# other failure is logged, remembered and counted against the budget.
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import assert_never


class Failure(StrEnum):
    WEDGED = "wedged"
    SCHEMA_DRIFT = "schema_drift"
    DATABASE_UNAVAILABLE = "database_unavailable"
    OTHER = "other"


class LogLevel(StrEnum):
    CRITICAL = "critical"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class Verdict:
    log: LogLevel | None
    record_error: bool
    count_failure: bool
    reset_budget: bool
    park: bool
    reraise: bool


# meiva:note
# `None` is a pass that completed.
def verdict(failure: Failure | None) -> Verdict:
    match failure:
        case None:
            return Verdict(
                log=None,
                record_error=False,
                count_failure=False,
                reset_budget=True,
                park=False,
                reraise=False,
            )
        case Failure.WEDGED:
            return Verdict(
                log=LogLevel.CRITICAL,
                record_error=False,
                count_failure=False,
                reset_budget=False,
                park=True,
                reraise=False,
            )
        case Failure.SCHEMA_DRIFT:
            return Verdict(
                log=LogLevel.CRITICAL,
                record_error=False,
                count_failure=False,
                reset_budget=False,
                park=False,
                reraise=True,
            )
        case Failure.DATABASE_UNAVAILABLE:
            return Verdict(
                log=LogLevel.WARNING,
                record_error=True,
                count_failure=False,
                reset_budget=False,
                park=False,
                reraise=False,
            )
        case Failure.OTHER:
            return Verdict(
                log=LogLevel.ERROR,
                record_error=True,
                count_failure=True,
                reset_budget=False,
                park=False,
                reraise=False,
            )
        case _:
            assert_never(failure)
