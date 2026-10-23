"""Concrete process logging startup: the process-level sinks and their idempotency guard.

The event-pipeline half (loguru record -> unified event, sampling, the gateway/CLI entry
points) lives in `base.telemetry.process_logging`, which composes this module's
`start_process_logging`; `base.log` never imports telemetry.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from base.log import logger
from base.log.sinks import HUMAN_FORMAT, add_file_sink, add_sink, install_stdlib_intercept

__all__ = ["init_subprocess_logger", "reset_init_done", "start_process_logging"]


# meiva:note
# Process-level idempotency guard. The init_* functions add
# sinks, but `logger.add` is not idempotent — repeated calls
# accumulate sinks until fd exhaustion (errno 24 Too many open
# files). Bug symptom: the watchdog daemon calls 6 healthcheck.main()
# every 60s, each main calls init_gateway_process() -> 18 sinks per
# minute, hourly accumulation ~1000 fd then crash.
#
# Make init functions idempotent (process-level singleton) — later
# repeated calls silent-skips, matching their docstring "called once
# at startup" contract.
init_done = False


def reset_init_done() -> None:
    # meiva:note
    # Reset the process-level startup guard for isolated tests.
    global init_done  # noqa: PLW0603
    init_done = False


def init_subprocess_logger(
    *,
    agent_id: int,
    configure_fn: Callable[..., object] | None = None,
    file_sink_fn: Callable[[Path], object] | None = None,
) -> None:
    """Called once at exec subprocess startup. **Only** adds the file
    sink, no stderr — the subprocess's stderr is captured by parent
    and injected into exec_output fed to the LLM; framework logs to
    stderr would pollute the agent context.

    Writes `agent-{N}.log`, which every exec subprocess of that agent appends
    to; `process_role="subprocess"` marks these records.

    Idempotent — repeat in the same process silent-skips (see
    ``init_done`` at module top).
    """
    global init_done  # noqa: PLW0603
    if init_done:
        return
    from base.paths import logs_dir

    configure = logger.configure if configure_fn is None else configure_fn
    add_file = add_file_sink if file_sink_fn is None else file_sink_fn
    configure(extra={"agent_id": str(agent_id), "process_role": "subprocess"})
    add_file(logs_dir() / f"agent-{agent_id}.log")
    install_stdlib_intercept()
    init_done = True


def _add_stderr_sink_before_settings() -> None:
    """The human stderr sink, opened before the init imports `base.paths`.

    That import builds the Settings chain, and the build logs: a configured
    runner's gateway fetch warns when it continues on a stale config snapshot
    (`base/host/env/bootstrap.py`). The fresh executable prelude removed loguru's default handler; open the
    human sink before configuration warnings can be emitted.
    """
    add_sink(sys.stderr, format=HUMAN_FORMAT, level="INFO", colorize=True)


def start_process_logging(
    name: str,
    *,
    attach_events: Callable[[], object],
    announce: Callable[[], None] | None = None,
    stderr_sink_fn: Callable[..., object] | None = None,
    file_sink_fn: Callable[[Path], object] | None = None,
    configure_fn: Callable[..., object] | None = None,
) -> None:
    """Open a long-running process's sinks once: stderr (human) + file (``<name>.log``)
    + the event sink ``attach_events`` registers + the stdlib intercept.

    The caller (telemetry owns the event pipeline) supplies ``attach_events`` so this
    module never imports it; ``announce`` runs after the sinks are live (a boot log line).
    Agent id is the ``-`` sentinel (no agent). Idempotent — a repeat in the same process
    silent-skips (see ``init_done``)."""
    global init_done  # noqa: PLW0603
    if init_done:
        return
    configure = logger.configure if configure_fn is None else configure_fn
    add_file = add_file_sink if file_sink_fn is None else file_sink_fn
    configure(extra={"agent_id": "-"})
    if stderr_sink_fn is None:
        _add_stderr_sink_before_settings()
    else:
        stderr_sink_fn(sys.stderr, format=HUMAN_FORMAT, level="INFO", colorize=True)
    from base.paths import logs_dir

    add_file(logs_dir() / f"{name}.log")
    attach_events()
    install_stdlib_intercept()
    if announce is not None:
        announce()
    init_done = True
