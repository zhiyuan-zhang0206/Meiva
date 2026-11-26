# meiva:link base/lm/usage/README.md

from collections.abc import Generator
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Connection, connect
from sqlite3 import Error as SQLiteError
from threading import Event, Lock, Thread

from loguru import logger
from pydantic import TypeAdapter

from base.api_contracts.usage import (
    ReceiptSubmission,
    UsageLedger,
    UsageLedgerConflictError,
    UsageLedgerEntry,
    UsageLedgerRejectedError,
)
from base.host.boundary.untyped import object_of

_ENTRY = TypeAdapter(UsageLedgerEntry)
_COUNT = TypeAdapter(tuple[int])
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
    busy_seconds: float = 0.05

    @property
    def path(self) -> Path:
        return self.home / "usage-receipts.v1.sqlite3"

    @contextmanager
    def _open(self) -> Generator[Connection]:
        with closing(connect(self.path, timeout=self.busy_seconds)) as conn, conn:
            _ = conn.execute("PRAGMA synchronous=EXTRA")
            _ = conn.execute("PRAGMA max_page_count=32768")
            _ = conn.execute(_SCHEMA)
            yield conn

    def stage(self, entry: UsageLedgerEntry) -> None:
        if entry.cluster != self.cluster:
            raise ValueError("usage receipt belongs to another cluster")
        self._stage(entry, encode_receipt(entry))

    def recover(self, payload: bytes) -> None:
        if len(payload) > self.max_payload_bytes:
            raise ReceiptCapacityError("usage receipt exceeds the payload limit")
        self.stage(decode_receipt(payload))

    def _stage(self, entry: UsageLedgerEntry, payload: bytes) -> None:
        if len(payload) > self.max_payload_bytes:
            raise ReceiptCapacityError("usage receipt exceeds the payload limit")
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

    def submit(self, entry: UsageLedgerEntry) -> ReceiptSubmission:
        try:
            self._store.stage(entry)
        except (OSError, SQLiteError) as exc:
            _log_fallback(self._store, entry, exc)
            return ReceiptSubmission.FALLBACK_LOGGED
        self._wake.set()
        return ReceiptSubmission.STAGED

    def stop(self, *, timeout: float) -> bool:
        with self._lifecycle_lock:
            self._stop_requested.set()
            thread = self._thread
        self._wake.set()
        if thread is None:
            return True
        thread.join(timeout=timeout)
        if thread.is_alive():
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
            _ = self._ledger.write(entry)
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


def _log_fallback(
    store: UsageReceiptStore, entry: UsageLedgerEntry, exc: OSError | SQLiteError
) -> None:
    backlog: ReceiptBacklog | None = None
    with suppress(OSError, SQLiteError):
        backlog = store.backlog()
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
