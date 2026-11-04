"""Concrete process logging startup: the process-level sinks and their idempotency guard.

The event-pipeline half (loguru record -> unified event, sampling, the gateway/CLI entry
points) lives in `base.telemetry.process_logging`, which composes this module's
`start_process_logging`; `base.log` never imports telemetry.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from base.log import logger
from base.log.sinks import HUMAN_FORMAT, add_file_sink, add_sink, install_stdlib_intercept

__all__ = [
    "buffer_startup_records",
    "flush_startup_records",
    "init_subprocess_logger",
    "start_process_logging",
]


# Process-level idempotency guard. The init_* functions add
# sinks, but `logger.add` is not idempotent — repeated calls
# accumulate sinks until fd exhaustion (errno 24 Too many open
# files). Bug symptom: the watchdog daemon calls 6 healthcheck.main()
# every 60s, each main calls init_gateway_process() -> 18 sinks per
# minute, hourly accumulation ~1000 fd then crash.
#
# Make init functions idempotent (process-level singleton) — later
# repeated calls silent-skip, matching their docstring "called once
# at startup" contract.
init_done = False


# meiva:note
# Called once at exec subprocess startup. **Only** adds the file
# sink, no stderr — the subprocess's stderr is captured by parent
# and injected into exec_output fed to the LLM; framework logs to
# stderr would pollute the agent context.
#
# Writes `agent-{N}.log`, which every exec subprocess of that agent appends
# to; `process_role="subprocess"` marks these records.
#
# ``home`` is the home whose ``logs/`` directory holds the file: the entry that resolved it passes it.
#
# Idempotent — repeat in the same process silent-skips (see
# ``init_done`` at module top).
def init_subprocess_logger(*, agent_id: int, home: Path) -> None:
    global init_done  # noqa: PLW0603
    if init_done:
        return
    from base.paths import logs_dir

    logger.configure(extra={"agent_id": str(agent_id), "process_role": "subprocess"})
    add_file_sink(logs_dir(home=home) / f"agent-{agent_id}.log")
    install_stdlib_intercept()
    init_done = True


# meiva:note
# The records a process emits before its sinks exist are the ones a reader needs most: they are the
# startup itself. On 2026-11-04 a whole generation of `unit … started` lines went to a stderr
# nobody reads, because the root spawns every unit before the participant that installs the file
# sink has started. Holding them here and replaying them into the sink keeps the message, the
# logger name and the instant each was written with.
_STARTUP_RECORD_LIMIT = 2000
_startup_records: list[logging.LogRecord] = []


class _StartupRecordBuffer(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        if len(_startup_records) < _STARTUP_RECORD_LIMIT:
            _startup_records.append(record)


# meiva:note
# Installed by an entry point right after `logging.basicConfig`, so everything logged while the
# process is still coming up is kept. Idempotent: a second call adds no second handler.
def buffer_startup_records() -> None:
    root = logging.getLogger()
    if any(isinstance(handler, _StartupRecordBuffer) for handler in root.handlers):
        return
    root.addHandler(_StartupRecordBuffer())


# meiva:note
# One replay, then the buffer is empty: the records already reached the sink, and a second call
# would duplicate them. `install_stdlib_intercept` replaces the root handlers (its own
# `basicConfig(force=True)`), which is what removes the buffer handler.
def flush_startup_records() -> None:
    records, _startup_records[:] = list(_startup_records), []
    for record in records:
        stamped = logger.patch(
            lambda entry, when=record.created, name=record.name: entry.update(
                time=datetime.fromtimestamp(when, tz=UTC), name=name
            )
        )
        stamped.opt(exception=record.exc_info).log(record.levelname, record.getMessage())


def _add_stderr_sink_before_settings() -> None:
    """The human stderr sink, opened before the init imports `base.paths`.

    That import builds the Settings chain, and the build logs: a configured
    runner's gateway fetch warns when it continues on a stale config snapshot
    (`base/host/env/bootstrap.py`). The fresh executable prelude removed loguru's default handler; open the
    human sink before configuration warnings can be emitted.
    """
    add_sink(sys.stderr, format=HUMAN_FORMAT, level="INFO", colorize=True)


# meiva:note
# Open a long-running process's sinks once: stderr (human) + file (``<name>.log``)
# + the event sink ``attach_events`` registers + the stdlib intercept.
#
# The caller (telemetry owns the event pipeline) supplies ``attach_events`` so this
# module never imports it; ``announce`` runs after the sinks are live (a boot log line).
# ``home`` is the home whose ``logs/`` directory holds the file: the entry that resolved it passes
# it. Agent id is the ``-`` sentinel (no agent). Idempotent — a repeat in the same process
# silent-skips (see ``init_done``).
def start_process_logging(
    name: str,
    *,
    attach_events: Callable[[], object],
    announce: Callable[[], None] | None = None,
    home: Path,
) -> None:
    global init_done  # noqa: PLW0603
    if init_done:
        return
    logger.configure(extra={"agent_id": "-"})
    _add_stderr_sink_before_settings()
    from base.paths import logs_dir

    add_file_sink(logs_dir(home=home) / f"{name}.log")
    attach_events()
    install_stdlib_intercept()
    flush_startup_records()
    if announce is not None:
        announce()
    init_done = True
