# meiva:link base/deploy/docs/start-trace.meiva.okf.md
# meiva:note
# `meiva start` is attributed phase by phase; the record is printed only when the
# run asks for it and is never read to decide anything.

from __future__ import annotations

import json
import time
from collections.abc import Generator, Mapping
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from pathlib import Path

_ENV = "MEIVA_START_TIMING"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def requested(environ: Mapping[str, str]) -> bool:
    return environ.get(_ENV, "").strip().lower() in _TRUTHY


class StartTrace:
    # meiva:note
    # The origin, the pid and the wall-clock start are set by `begin()` at the CLI entry, so a
    # row's start offset is measured from command dispatch — not from this module's import (the
    # interpreter's import of the CLI is outside every span) — and the saved record is named for
    # the process that ran it and the moment it started.
    def __init__(self, *, enabled: bool = False) -> None:
        self.enabled = enabled
        self.spans: list[tuple[str, float, float]] = []
        self.services: list[tuple[str, float]] = []
        self.origin: float | None = None
        self.pid: int | None = None
        self.started_utc: datetime | None = None
        self._open: list[float] = []

    def begin(self, *, pid: int, now: datetime, enabled: bool = False) -> None:
        if now.tzinfo is None:
            raise ValueError("the start time of a timing record must carry a timezone")
        self.enabled = self.enabled or enabled
        self.pid = pid
        self.started_utc = now.astimezone(UTC)
        if self.origin is None:
            self.origin = time.monotonic()

    def started_at(self) -> float:
        origin = self.origin
        if origin is None:
            origin = time.monotonic()
            self.origin = origin
        return origin

    def phase(self, name: str) -> AbstractContextManager[None]:
        return self._phase(name)

    @contextmanager
    def _phase(self, name: str) -> Generator[None]:
        origin = self.started_at()
        started = time.monotonic()
        self._open.append(started)
        try:
            yield
        finally:
            self._open.pop()
            self.spans.append((name, started - origin, time.monotonic() - started))

    def service_ready(self, name: str, seconds: float) -> None:
        self.services.append((name, seconds))

    def total_s(self) -> float:
        return time.monotonic() - self.started_at()

    def rows(self) -> list[tuple[str, float, float]]:
        return sorted(self.spans, key=lambda span: span[1])

    def render(self) -> str:
        rows = self.rows()
        lines = [f"start timing ({_ENV}=1; seconds from the CLI entry)"]
        lines.append(f"  {'start':>7} {'took':>8}  phase")
        for name, start, took in rows:
            lines.append(f"  {start:7.2f} {took:8.2f}  {name}")
        for name, seconds in self.services:
            lines.append(f"  {seconds:7.2f} {'':8}  ready: {name}")
        lines.append(f"  {self.total_s():7.2f} {'':8}  total")
        return "\n".join(lines)

    def payload(self) -> dict[str, object]:
        return {
            "total_s": round(self.total_s(), 3),
            "phases": [
                {"name": name, "start_s": round(start, 3), "seconds": round(took, 3)}
                for name, start, took in self.rows()
            ],
            "service_ready_s": {name: round(seconds, 3) for name, seconds in self.services},
        }

    def write(self, home: Path) -> Path | None:
        # meiva:note
        # A start that was refused must not leave a home behind, so a report is
        # only persisted under a home that already exists.
        if not home.is_dir():
            return None
        pid, started = self.pid, self.started_utc
        if pid is None or started is None:
            raise RuntimeError("a start timing record is saved only after begin(pid=..., now=...)")
        try:
            target = home / "logs" / "start-timing"
            target.mkdir(parents=True, exist_ok=True)
            stamp = started.strftime("%Y%m%dT%H%M%SZ")
            path = target / f"{stamp}-{pid}.json"
            path.write_text(json.dumps(self.payload(), indent=2) + "\n")
            return path
        except OSError:
            return None


_TRACE = StartTrace()


def trace() -> StartTrace:
    return _TRACE


def begin(*, pid: int, now: datetime, enabled: bool = False) -> None:
    _TRACE.begin(pid=pid, now=now, enabled=enabled)


def phase(name: str) -> AbstractContextManager[None]:
    return _TRACE.phase(name)


def record_service_ready(name: str, seconds: float) -> None:
    _TRACE.service_ready(name, seconds)


def report(home: Path | None = None, *, requested: bool | None = None) -> str | None:
    # meiva:note
    # The caller prints; nothing in `base` writes to stdout. The JSON copy is kept
    # beside the logs so before/after runs can be compared.
    if not (_TRACE.enabled if requested is None else requested):
        return None
    text = _TRACE.render()
    if home is not None:
        path = _TRACE.write(home)
        if path is not None:
            text += f"\n  saved: {path}"
    return text
