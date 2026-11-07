# meiva:note
# No sink in this process renders a traceback frame's local variables.
#
# loguru's `diagnose` defaults to True: on a record that carries an exception it inlines the value of
# every local variable of every frame in the traceback, next to the source line that used it.
# `add_sink` opens every sink this repository creates with `diagnose=False`, but the sinks we open are
# not the only ones a process has. loguru creates its own default stderr handler at `import loguru`,
# before any of our code runs, and every process that never calls `prepare_process_logging()` keeps
# it: an exec host or the MCP daemon that opens its own sink, the pty session server, and any script
# that imports the SDK. That handler writes to whatever stderr is, which for a supervised unit is
# `<home>/run/*/logs/<unit>/output.log`.
#
# `base.log.redaction` masks what it can recognise by shape. `diagnose` renders values under names
# nothing can interpret -- a password held in a variable called `x` matches no rule -- so the quantity
# has to close too: not "mostly masked", but "no handler renders frame variables at all".
#
# It is installed at the seam that opens a sink, like the redaction patcher beside it: `add_sink`
# disables frame-variable rendering on every handler registered before the sink it is opening, which
# covers the default one in a process that never runs the prelude. The coverage is therefore exactly
# redaction's: a process that logs without opening a sink -- an ad-hoc script that imports the SDK,
# nothing in this repository -- gets neither. A call at import would reach it and is not available:
# meizel's `import-time-call` rule refuses a side effect that belongs to importing a module.
#
# The remaining way in is a `logger.add` called directly, which no runtime hook sees before the record
# is already formatted. That one is refused in source instead:
# `scripts/lint/diagnostics/logger_add_diagnose.py` allows the call only inside `base/log/`.
#
# loguru exposes no accessor for the registry or for a handler's flag, so both are read through its
# private attributes and validated there; a guard that cannot see what it is guarding raises instead
# of reporting a safety nobody checked.

from __future__ import annotations

from importlib.metadata import version
from typing import Protocol

from loguru import logger
from loguru._handler import Handler

__all__ = ["diagnosing_handler_ids", "disable_frame_variable_rendering"]


class _ExceptionFormatter(Protocol):
    # meiva:note
    # The one attribute of loguru's exception formatter this module reads. loguru publishes no type
    # for it, so the protocol names the shape the guard depends on instead of typing it `object`.
    _diagnose: bool


def _handler_items() -> list[tuple[int, Handler]]:
    registry = getattr(getattr(logger, "_core", None), "handlers", None)
    if registry is None:
        raise RuntimeError(
            f"loguru {version('loguru')} no longer exposes its handler registry where "
            "base.log.diagnose reads it: update this module to the new location before any process "
            "claims its sinks do not render frame variables"
        )
    return [(int(handler_id), handler) for handler_id, handler in dict(registry).items()]


def _formatter(handler: Handler) -> _ExceptionFormatter:
    formatter = getattr(handler, "_exception_formatter", None)
    if formatter is None:
        raise RuntimeError(
            f"a loguru {version('loguru')} handler has no exception formatter: base.log.diagnose "
            "cannot tell whether this sink renders frame variables, and a sink nobody can inspect "
            "must not be reported as safe"
        )
    return formatter


def _diagnose_of(formatter: _ExceptionFormatter) -> bool:
    diagnose = getattr(formatter, "_diagnose", None)
    if diagnose is None:
        raise RuntimeError(
            f"loguru {version('loguru')} no longer exposes a handler's `diagnose` flag where "
            "base.log.diagnose reads it: update this module to the new location before any process "
            "claims its sinks do not render frame variables"
        )
    return bool(diagnose)


# meiva:note
# The invariant read as a set of ids rather than a predicate, so a failure names the unsafe sinks
# instead of saying one exists.
def diagnosing_handler_ids() -> list[int]:
    return [
        handler_id for handler_id, handler in _handler_items() if _diagnose_of(_formatter(handler))
    ]


# meiva:note
# Idempotent, and it changes the handler in place rather than removing and re-adding it: a process
# that calls this before opening a sink keeps its stderr output, it just no longer carries the state
# of the failing frames.
def disable_frame_variable_rendering() -> list[int]:
    changed: list[int] = []
    for handler_id, handler in _handler_items():
        formatter = _formatter(handler)
        if _diagnose_of(formatter):
            # meiva:note
            # The flag belongs to a third-party object with no setter of its own. Writing it is the
            # whole point of this module, and the handler keeps every other setting it was registered
            # with -- format, level, filter and the colorize decision its owner configured.
            setattr(formatter, "_diagnose", False)  # noqa: B010
            changed.append(handler_id)
    return changed
