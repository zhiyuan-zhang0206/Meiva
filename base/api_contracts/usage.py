# meiva:link base/lm/usage/README.md
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol


@dataclass(frozen=True, slots=True)
class UsageLedgerEntry:
    call_id: str
    occurred_at: datetime
    agent_id: int | None
    machine: str
    cluster: str
    source: str | None
    billing: str
    usage_kind: str
    model: str
    requested_model: str | None
    provider: str | None
    provider_request_id: str | None
    calls: int
    complete: bool
    tokens_in: int | None
    tokens_out: int | None
    tokens_reasoning: int | None
    cache_read: int | None
    cache_write_5m: int | None
    cache_write_1h: int | None
    cache_write_30m: int | None
    cost_usd: Decimal | None
    price_snapshot: dict[str, float | str]
    price_source: str | None


class UsageLedgerWriteError(RuntimeError):
    pass


class UsageLedgerConflictError(UsageLedgerWriteError):
    pass


class UsageLedgerRejectedError(UsageLedgerWriteError):
    pass


@dataclass(frozen=True, slots=True)
class UsageLedger:
    write: Callable[[UsageLedgerEntry], bool]
    machine: Callable[[], str]
    cluster: str
    new_call_id: Callable[[], str]


class ReceiptSubmission(StrEnum):
    STAGED = "staged"
    FALLBACK_LOGGED = "fallback_logged"


type ReceiptFallbackReporter = Callable[[Callable[[], None]], bool]


class UsageReceiptSubmitter(Protocol):
    def __call__(
        self,
        entry: UsageLedgerEntry,
        /,
        *,
        report_fallback: ReceiptFallbackReporter | None = None,
    ) -> ReceiptSubmission: ...


type ScheduleUsageReceipt = Callable[
    [UsageLedgerEntry, UsageReceiptSubmitter, Callable[[], None]], Future[None]
]
type ReserveUsageReceipt = Callable[[], AbstractContextManager[ScheduleUsageReceipt]]


@dataclass(frozen=True, slots=True)
class ScheduledUsageReceipt:
    write: UsageReceiptSubmitter
    schedule: ScheduleUsageReceipt


class ReceiptStagingUnavailableError(RuntimeError):
    pass
