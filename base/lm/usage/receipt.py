# meiva:link base/lm/usage/README.md

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from threading import Event, Thread

from loguru import logger
from pydantic import TypeAdapter

from base.api_contracts.usage import UsageLedger, UsageLedgerConflictError, UsageLedgerEntry
from base.host.atomic_io import fsync_parent, write_bytes_atomic

_ENTRY = TypeAdapter(UsageLedgerEntry)
_PENDING_SUFFIX = ".v1.json"
_DEFAULT_RETRY_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class UsageReceiptStore:
    directory: Path

    def stage(self, entry: UsageLedgerEntry) -> None:
        data = _ENTRY.dump_json(entry)
        path = self.directory / f"{sha256(data).hexdigest()}{_PENDING_SUFFIX}"
        self.directory.mkdir(mode=0o700, exist_ok=True)
        fsync_parent(self.directory)
        write_bytes_atomic(path, data, mode=0o600, sync_parent=True)

    def replay(self, ledger: UsageLedger) -> bool:
        try:
            for path in self.directory.iterdir():
                if path.name.endswith(_PENDING_SUFFIX) and not _post(path, ledger):
                    return False
        except FileNotFoundError:
            return True
        except OSError as exc:
            logger.error("Cannot scan usage receipts in {}: {}", self.directory, exc)
            return False
        return True


class UsageReceiptDelivery:
    def __init__(
        self,
        store: UsageReceiptStore,
        ledger: UsageLedger,
        *,
        retry_seconds: float = _DEFAULT_RETRY_SECONDS,
    ) -> None:
        self._store = store
        self._ledger = ledger
        self._retry_seconds = retry_seconds
        self._stop_requested = Event()
        self._ready_to_replay = Event()
        self._error: BaseException | None = None
        self._thread = Thread(target=self._run, name="usage-receipts", daemon=True)
        self._thread.start()

    def submit(self, entry: UsageLedgerEntry) -> None:
        try:
            self._store.stage(entry)
        except OSError as exc:
            logger.error(
                "Usage receipt storage failed for {call_id} in {directory} ({error_type}); "
                "recover from the accompanying llm_usage event",
                call_id=entry.call_id,
                directory=str(self._store.directory),
                error_type=type(exc).__name__,
            )
            return
        self._ready_to_replay.set()

    def stop(self, *, timeout: float) -> bool:
        self._stop_requested.set()
        self._ready_to_replay.set()
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            logger.warning(
                "Usage receipt worker stopping with possible pending files in {}",
                self._store.directory,
            )
            return False
        if self._error is not None:
            raise self._error
        return True

    def _run(self) -> None:
        try:
            self._consume()
        except BaseException as exc:
            self._error = exc
            logger.opt(exception=exc).error(
                "Usage receipt worker stopped for {}", self._store.directory
            )

    def _consume(self) -> None:
        while not self._stop_requested.is_set():
            self._ready_to_replay.clear()
            if self._stop_requested.is_set():
                return
            if self._store.replay(self._ledger):
                _ = self._ready_to_replay.wait()
            else:
                _ = self._stop_requested.wait(self._retry_seconds)


def _read(path: Path) -> UsageLedgerEntry:
    data = path.read_bytes()
    if path.name != f"{sha256(data).hexdigest()}{_PENDING_SUFFIX}":
        raise ValueError("usage receipt checksum mismatch")
    return _ENTRY.validate_json(data, strict=True)


def _post(path: Path, ledger: UsageLedger) -> bool:
    try:
        entry = _read(path)
    except FileNotFoundError:
        return True
    except ValueError as exc:
        _retain_conflict(path, None, reason=type(exc).__name__)
        return True
    if entry.cluster != ledger.cluster:
        _retain_conflict(path, entry, reason="cluster_mismatch")
        return True
    try:
        _ = ledger.write(entry)
        path.unlink(missing_ok=True)
    except UsageLedgerConflictError:
        _retain_conflict(path, entry, reason="ledger_conflict")
    except Exception as exc:
        logger.warning(
            "Usage receipt pending at {receipt} for {call_id} ({error_type}); "
            "ledger delivery will retry",
            receipt=str(path),
            call_id=entry.call_id,
            error_type=type(exc).__name__,
        )
        return False
    return True


def _retain_conflict(path: Path, entry: UsageLedgerEntry | None, *, reason: str) -> None:
    conflict = path.with_suffix(".conflict")
    try:
        _ = path.replace(conflict)
    except FileNotFoundError:
        return
    fsync_parent(conflict)
    logger.error(
        "Usage receipt conflict retained at {receipt} for {call_id}: {reason}",
        receipt=str(conflict),
        call_id=entry.call_id if entry is not None else None,
        reason=reason,
    )
