# meiva:link base/lm/usage/README.md

import asyncio
from collections.abc import Callable, Generator
from concurrent.futures import Future
from contextlib import AbstractContextManager, closing, contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path
from queue import SimpleQueue
from sqlite3 import SQLITE_BUSY, SQLITE_LOCKED, Connection, OperationalError, connect
from sqlite3 import Error as SQLiteError
from threading import Event, Lock, Thread

from loguru import logger
from opentelemetry import context as trace_context
from pydantic import TypeAdapter

from base.api_contracts.usage import (
    ReceiptFallbackReporter,
    ReceiptStagingUnavailableError,
    ReceiptSubmission,
    ScheduledUsageReceipt,
    ScheduleUsageReceipt,
    UsageLedger,
    UsageLedgerConflictError,
    UsageLedgerEntry,
    UsageLedgerRejectedError,
    UsageReceiptSubmitter,
)
from base.host.boundary.untyped import object_of
from base.host.net.resilience import retry_sleep
from base.lm.catalog import ModelCatalog
from base.lm.usage import UsageFields, log_usage_fields

_ENTRY = TypeAdapter(UsageLedgerEntry)
_COUNT = TypeAdapter(tuple[int])
_JOURNAL_MODE = TypeAdapter(tuple[str])
_BUSY_RETRY_DELAYS = (0.02, 0.05, 0.1)
_PENDING = TypeAdapter(list[tuple[int, bytes]])
_BACKLOG = TypeAdapter(tuple[int, int, str | None])
_SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    payload BLOB NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL,
    rejected INTEGER NOT NULL DEFAULT 0
)
"""


def encode_receipt(entry: UsageLedgerEntry) -> bytes:
    return _ENTRY.dump_json(entry)


def decode_receipt(payload: bytes) -> UsageLedgerEntry:
    return _ENTRY.validate_json(payload, strict=True)


class ReceiptCapacityError(OSError):
    pass


@dataclass(frozen=True, slots=True)
class ReceiptBacklog:
    total: int
    pending: int
    oldest_pending: str | None
    available: int


@dataclass(frozen=True, slots=True)
class UsageReceiptStore:
    home: Path
    cluster: str
    max_receipts: int = 4096
    max_payload_bytes: int = 8192
    busy_seconds: float = 5.0

    @property
    def path(self) -> Path:
        return self.home / "usage-receipts.v1.sqlite3"

    @contextmanager
    def _open(self) -> Generator[Connection]:
        with closing(connect(self.path, timeout=self.busy_seconds)) as conn, conn:
            journal_mode = conn.execute("PRAGMA journal_mode")
            (mode,) = _JOURNAL_MODE.validate_python(object_of(journal_mode.fetchone), strict=True)
            if mode != "wal":
                changed = conn.execute("PRAGMA journal_mode=WAL")
                (mode,) = _JOURNAL_MODE.validate_python(object_of(changed.fetchone), strict=True)
                if mode != "wal":
                    raise OSError("usage receipt spool requires SQLite WAL mode")
            _ = conn.execute("PRAGMA synchronous=FULL")
            _ = conn.execute("PRAGMA max_page_count=32768")
            _ = conn.execute(_SCHEMA)
            yield conn

    def stage(self, entry: UsageLedgerEntry) -> None:
        payload = encode_receipt(entry)
        if len(payload) > self.max_payload_bytes:
            raise ReceiptCapacityError("usage receipt exceeds the automatic payload limit")
        self._stage(entry, payload)

    def recover(self, payload: bytes) -> None:
        self._stage(decode_receipt(payload), payload)

    def _stage(self, entry: UsageLedgerEntry, payload: bytes) -> None:
        if entry.cluster != self.cluster:
            raise ValueError("usage receipt belongs to another cluster")
        for delay in _BUSY_RETRY_DELAYS:
            try:
                self._insert(entry, payload)
            except OperationalError as exc:
                if getattr(exc, "sqlite_errorcode", 0) & 0xFF not in (SQLITE_BUSY, SQLITE_LOCKED):
                    raise
                retry_sleep(delay)
            else:
                return
        self._insert(entry, payload)

    def _insert(self, entry: UsageLedgerEntry, payload: bytes) -> None:
        with self._open() as conn:
            _ = conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM receipts WHERE payload = ?", (payload,)).fetchone():
                return
            row = object_of(conn.execute("SELECT count(*) FROM receipts").fetchone)
            (total,) = _COUNT.validate_python(row, strict=True)
            if total >= self.max_receipts:
                raise ReceiptCapacityError("usage receipt spool is full")
            _ = conn.execute(
                "INSERT INTO receipts (payload, occurred_at) VALUES (?, ?)",
                (payload, entry.occurred_at.isoformat()),
            )

    def pending(self, *, after: int, limit: int) -> list[tuple[int, bytes]]:
        if not self.path.exists():
            return []
        with self._open() as conn:
            rows = conn.execute(
                "SELECT seq, payload FROM receipts WHERE seq > ? AND rejected = 0 "
                "ORDER BY seq LIMIT ?",
                (after, limit),
            )
            return _PENDING.validate_python(object_of(rows.fetchall), strict=True)

    def acknowledge(self, seq: int) -> None:
        with self._open() as conn:
            _ = conn.execute("DELETE FROM receipts WHERE seq = ?", (seq,))

    def reject(self, seq: int) -> None:
        with self._open() as conn:
            _ = conn.execute("UPDATE receipts SET rejected = 1 WHERE seq = ?", (seq,))

    def backlog(self) -> ReceiptBacklog:
        with self._open() as conn:
            row = conn.execute(
                "SELECT count(*), count(*) FILTER (WHERE rejected = 0), "
                "min(occurred_at) FILTER (WHERE rejected = 0) FROM receipts"
            )
            total, pending, oldest = _BACKLOG.validate_python(object_of(row.fetchone), strict=True)
        return ReceiptBacklog(total, pending, oldest, self.max_receipts - total)


class UsageReceiptDelivery:
    def __init__(
        self,
        store: UsageReceiptStore,
        ledger: UsageLedger,
        *,
        poll_seconds: float = 30.0,
        batch_size: int = 16,
    ) -> None:
        if store.cluster != ledger.cluster:
            raise ValueError("usage receipt store and ledger must share ownership")
        self._store = store
        self._ledger = ledger
        self._poll_seconds = poll_seconds
        self._batch_size = batch_size
        self._lifecycle_lock = Lock()
        self._stop_requested = Event()
        self._wake = Event()
        self._error: BaseException | None = None
        self._thread: Thread | None = None

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._stop_requested.is_set():
                raise RuntimeError("usage receipt delivery already stopped")
            if self._thread is not None:
                return
            self._thread = Thread(target=self._run, name="usage-receipts", daemon=True)
        self._thread.start()

    def submit(
        self, entry: UsageLedgerEntry, *, report_fallback: ReceiptFallbackReporter | None = None
    ) -> ReceiptSubmission:
        result = _submit(self._store, entry, report_fallback=report_fallback)
        if result is ReceiptSubmission.STAGED:
            self._wake.set()
        return result

    def _request_stop(self) -> None:
        with self._lifecycle_lock:
            self._stop_requested.set()
        self._wake.set()

    def stop(self, *, timeout: float) -> bool:
        self._request_stop()
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=timeout)
        return self._finish_stop()

    async def aclose(self, *, timeout: float) -> None:
        self._request_stop()
        thread = self._thread
        if thread is not None:
            await _wait_for_thread(thread, timeout=timeout)
            _ = self._finish_stop()

    def _finish_stop(self) -> bool:
        thread = self._thread
        if thread is not None and thread.is_alive():
            logger.warning("Usage receipt delivery stop unfinished at {}", self._store.path)
            return False
        if self._error is not None:
            raise self._error
        return True

    def _run(self) -> None:
        try:
            self._consume()
        except BaseException as exc:
            self._error = exc
            logger.opt(exception=exc).error("Usage receipt worker stopped at {}", self._store.path)

    def _consume(self) -> None:
        after = 0
        while not self._stop_requested.is_set():
            self._wake.clear()
            try:
                after, pause = self._replay_batch(after)
            except (OSError, SQLiteError) as exc:
                logger.error(
                    "Usage receipt scan failed at {} ({})", self._store.path, type(exc).__name__
                )
                pause = True
            if pause and not self._stop_requested.is_set():
                _ = self._wake.wait(self._poll_seconds)

    def _replay_batch(self, after: int) -> tuple[int, bool]:
        rows = self._store.pending(after=after, limit=self._batch_size)
        if not rows:
            return 0, after == 0
        delayed = False
        for seq, payload in rows:
            if self._stop_requested.is_set():
                break
            if not self._post(seq, payload):
                delayed = True
        if delayed:
            backlog = self._store.backlog()
            logger.warning(
                "Usage receipts pending at {spool}: {pending} pending, {available} slots available, "
                "oldest {oldest}",
                spool=str(self._store.path),
                pending=backlog.pending,
                available=backlog.available,
                oldest=backlog.oldest_pending,
            )
            _ = self._stop_requested.wait(self._poll_seconds)
        return rows[-1][0], False

    def _post(self, seq: int, payload: bytes) -> bool:
        try:
            entry = decode_receipt(payload)
        except ValueError:
            self._reject(seq, reason="invalid_payload")
            return True
        if entry.cluster != self._ledger.cluster:
            logger.error("Usage receipt {} at {} belongs to another cluster", seq, self._store.path)
            return False
        try:
            _ = self._ledger.write_usage_row(entry)
        except (UsageLedgerConflictError, UsageLedgerRejectedError) as exc:
            self._reject(seq, reason=type(exc).__name__)
        except Exception as exc:
            logger.warning(
                "Usage receipt {} pending at {} ({}); ledger delivery will retry",
                entry.call_id,
                self._store.path,
                type(exc).__name__,
                call_id=entry.call_id,
            )
            return False
        else:
            self._store.acknowledge(seq)
        return True

    def _reject(self, seq: int, *, reason: str) -> None:
        self._store.reject(seq)
        logger.error("Usage receipt {} quarantined at {} ({})", seq, self._store.path, reason)


def _report_unstaged_receipt(
    entry: UsageLedgerEntry,
    /,
    *,
    report_fallback: ReceiptFallbackReporter | None = None,
) -> ReceiptSubmission:
    emit = partial(_log_unconfirmed, entry, stage="staging_admission")
    if report_fallback is None:
        emit()
    else:
        _ = report_fallback(emit)
    return ReceiptSubmission.FALLBACK_LOGGED


def log_receipted_usage_fields(
    model: str,
    fields: UsageFields,
    *,
    usage_kind: str,
    catalog: ModelCatalog,
    now: datetime,
    submit_receipt: UsageReceiptSubmitter | ScheduledUsageReceipt | None,
) -> tuple[int, float]:
    return log_usage_fields(
        model,
        fields,
        usage_kind=usage_kind,
        catalog=catalog,
        now=now,
        submit_receipt=submit_receipt if submit_receipt is not None else _report_unstaged_receipt,
    )


def _submit(
    store: UsageReceiptStore,
    entry: UsageLedgerEntry,
    *,
    report_fallback: ReceiptFallbackReporter | None,
) -> ReceiptSubmission:
    try:
        store.stage(entry)
    except (OSError, SQLiteError) as exc:
        _log_fallback(store, entry, exc, report_fallback=report_fallback)
        return ReceiptSubmission.FALLBACK_LOGGED
    return ReceiptSubmission.STAGED


def _log_fallback(
    store: UsageReceiptStore,
    entry: UsageLedgerEntry,
    exc: OSError | SQLiteError,
    *,
    report_fallback: ReceiptFallbackReporter | None,
) -> None:
    backlog: ReceiptBacklog | None = None
    with suppress(OSError, SQLiteError):
        backlog = store.backlog()

    def emit() -> None:
        logger.error(
            "Usage receipt staging failed for {call_id} at {spool} ({error_type}); recover receipt_json",
            call_id=entry.call_id,
            spool=str(store.path),
            stage="local_spool",
            receipt_status=ReceiptSubmission.FALLBACK_LOGGED.value,
            error_type=type(exc).__name__,
            errno=exc.errno if isinstance(exc, OSError) else getattr(exc, "sqlite_errorcode", None),
            pending=backlog.pending if backlog is not None else None,
            oldest=backlog.oldest_pending if backlog is not None else None,
            available=backlog.available if backlog is not None else None,
            receipt_json=encode_receipt(entry).decode(),
        )

    if report_fallback is None:
        emit()
    else:
        _ = report_fallback(emit)


async def _wait_for_thread(thread: Thread, *, timeout: float) -> None:
    with suppress(TimeoutError):
        async with asyncio.timeout(timeout):
            while thread.is_alive():
                await asyncio.sleep(0.01)


def _log_unconfirmed(entry: UsageLedgerEntry, *, stage: str) -> None:
    logger.error(
        "Usage receipt {call_id} unconfirmed during {stage}; recover receipt_json={receipt_json}",
        call_id=entry.call_id,
        stage=stage,
        receipt_status=ReceiptSubmission.FALLBACK_LOGGED.value,
        receipt_json=encode_receipt(entry).decode(),
    )


class _FallbackOnce:
    def __init__(self) -> None:
        self._lock = Lock()
        self._emitted = False

    def emit(self, log: Callable[[], None]) -> bool:
        with self._lock:
            if self._emitted:
                return False
            log()
            self._emitted = True
            return True


@dataclass(frozen=True, slots=True)
class _StagingJob:
    entry: UsageLedgerEntry
    write: UsageReceiptSubmitter
    observe: Callable[[], None]
    result: Future[None]
    trace: trace_context.Context
    fallback: _FallbackOnce = field(default_factory=_FallbackOnce)

    def log_unconfirmed(self, *, stage: str) -> bool:
        return self.fallback.emit(partial(_log_unconfirmed, self.entry, stage=stage))


class UsageReceiptStaging:
    def __init__(self, *, max_outstanding: int = 256) -> None:
        self._max_outstanding = max_outstanding
        self._outstanding = 0
        self._jobs: SimpleQueue[_StagingJob | None] = SimpleQueue()
        self._pending: dict[Future[None], _StagingJob] = {}
        self._expired = False
        self._lock = Lock()
        self._stop_requested = Event()
        self._error: BaseException | None = None
        self._thread = Thread(target=self._run, name="usage-receipt-staging", daemon=True)
        self._thread.start()

    @contextmanager
    def reserve(self) -> Generator[ScheduleUsageReceipt]:
        with self._lock:
            if self._stop_requested.is_set() or self._error is not None:
                raise ReceiptStagingUnavailableError("usage receipt staging is stopped")
            if self._outstanding >= self._max_outstanding:
                raise ReceiptStagingUnavailableError("usage receipt staging is at capacity")
            self._outstanding += 1
        scheduled = False

        def schedule(
            entry: UsageLedgerEntry, write: UsageReceiptSubmitter, observe: Callable[[], None]
        ) -> Future[None]:
            nonlocal scheduled
            if scheduled:
                raise RuntimeError("usage receipt slot was already scheduled")
            scheduled = True
            job = _StagingJob(entry, write, observe, Future(), trace_context.get_current())
            return self._schedule(job)

        try:
            yield schedule
        finally:
            if not scheduled:
                self._release()

    def _schedule(self, job: _StagingJob) -> Future[None]:
        with self._lock:
            if not self._expired:
                self._pending[job.result] = job
                self._jobs.put(job)
                return job.result
        try:
            _ = job.log_unconfirmed(stage="shutdown_drain")
            job.result.set_result(None)
        finally:
            self._release()
        return job.result

    def _release(self) -> None:
        with self._lock:
            self._outstanding -= 1
            if self._outstanding == 0 and self._stop_requested.is_set():
                self._jobs.put(None)

    def _request_stop(self) -> None:
        with self._lock:
            self._stop_requested.set()
            if self._outstanding == 0:
                self._jobs.put(None)

    def stop(self, *, timeout: float) -> bool:
        self._request_stop()
        self._thread.join(timeout=timeout)
        return self._finish_stop()

    async def aclose(self, *, timeout: float) -> None:
        self._request_stop()
        try:
            await _wait_for_thread(self._thread, timeout=timeout)
        finally:
            _ = self._finish_stop()

    def _finish_stop(self) -> bool:
        with self._lock:
            self._expired = True
            unconfirmed = tuple(self._pending.values())
            self._pending.clear()
        logged = sum(job.log_unconfirmed(stage="shutdown_drain") for job in unconfirmed)
        if self._thread.is_alive():
            logger.warning(
                "Usage receipt staging stop unfinished ({} unconfirmed receipts logged)",
                logged,
            )
            return False
        if self._error is not None:
            raise self._error
        return True

    def _run(self) -> None:
        try:
            while not self._stop_requested.is_set() or self._outstanding:
                job = self._jobs.get()
                if job is None:
                    return
                self._execute(job)
        except BaseException as exc:
            self._error = exc
            logger.opt(exception=exc).error("Usage receipt staging worker stopped")

    def _execute(self, job: _StagingJob) -> None:
        running = job.result.set_running_or_notify_cancel()
        try:
            self._stage_and_observe(job)
        except Exception as exc:
            if running:
                job.result.set_exception(exc)
            logger.opt(exception=exc).error("Accepted provider usage receipt staging failed")
        else:
            if running:
                job.result.set_result(None)
        finally:
            self._release()

    def _stage_and_observe(self, job: _StagingJob) -> None:
        with self._lock:
            if job.result not in self._pending:
                return
        token = trace_context.attach(job.trace)
        try:
            self._write_receipt(job)
            job.observe()
        finally:
            trace_context.detach(token)

    def _write_receipt(self, job: _StagingJob) -> None:
        try:
            _ = job.write(job.entry, report_fallback=job.fallback.emit)
        except BaseException:
            _ = job.log_unconfirmed(stage="staging_error")
            raise
        finally:
            with self._lock:
                _ = self._pending.pop(job.result, None)


class UsageReceiptRuntime:
    def __init__(self, store: UsageReceiptStore, ledger: UsageLedger) -> None:
        self._store = store
        self._ledger = ledger
        self._lock = Lock()
        self._owners = 0
        self._staging: UsageReceiptStaging | None = None
        self._delivery: UsageReceiptDelivery | None = None

    def start(self) -> None:
        with self._lock:
            if self._owners == 0:
                delivery = UsageReceiptDelivery(self._store, self._ledger)
                delivery.start()
                try:
                    self._staging = UsageReceiptStaging()
                except BaseException:
                    _ = delivery.stop(timeout=1.0)
                    raise
                self._delivery = delivery
            self._owners += 1

    def _retire(self) -> tuple[UsageReceiptStaging | None, UsageReceiptDelivery | None]:
        with self._lock:
            self._owners -= 1
            if self._owners:
                return None, None
            staging, delivery = self._staging, self._delivery
            self._staging = self._delivery = None
            return staging, delivery

    async def aclose(self, *, staging_timeout: float, delivery_timeout: float) -> None:
        staging, delivery = self._retire()
        try:
            if staging is not None:
                await staging.aclose(timeout=staging_timeout)
        finally:
            if delivery is not None:
                await delivery.aclose(timeout=delivery_timeout)

    def schedule_completed(
        self, entry: UsageLedgerEntry, write: UsageReceiptSubmitter, observe: Callable[[], None]
    ) -> Future[None]:
        try:
            with self.reserve() as schedule:
                return schedule(entry, write, observe)
        except ReceiptStagingUnavailableError:
            _log_unconfirmed(entry, stage="staging_admission")
            observe()
            result: Future[None] = Future()
            result.set_result(None)
            return result

    def reserve(self) -> AbstractContextManager[ScheduleUsageReceipt]:
        with self._lock:
            if self._staging is None:
                raise ReceiptStagingUnavailableError(
                    "usage receipt staging has no active service owner"
                )
            return self._staging.reserve()

    def submit(
        self, entry: UsageLedgerEntry, *, report_fallback: ReceiptFallbackReporter | None = None
    ) -> ReceiptSubmission:
        with self._lock:
            delivery = self._delivery
        if delivery is None:
            return _submit(self._store, entry, report_fallback=report_fallback)
        return delivery.submit(entry, report_fallback=report_fallback)
