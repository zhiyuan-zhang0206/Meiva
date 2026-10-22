# meiva:note
# Render exec tracebacks for the agent (`<agent_code>` frames only) and for server logs (unfiltered).
# `register_agent_source` registers the source in `linecache` inside the disposable exec child.

from __future__ import annotations

import linecache
import re
import traceback
from traceback import StackSummary, TracebackException

AGENT_CODE_FILENAME = "<agent_code>"

_CARET_LINE_RE = re.compile(r"^\s*[ ~^]+\s*$")


def _strip_caret_lines(text: str) -> str:
    return "".join(
        line for line in text.splitlines(keepends=True) if not _CARET_LINE_RE.match(line)
    )


_TB_HEADER = "Traceback (most recent call last):\n"
_CAUSE_MESSAGE = "\nThe above exception was the direct cause of the following exception:\n\n"
_CONTEXT_MESSAGE = "\nDuring handling of the above exception, another exception occurred:\n\n"
_SYNTAX_EXC_NAMES = frozenset({"SyntaxError", "IndentationError", "TabError"})


def register_agent_source(code: str) -> None:
    linecache.cache[AGENT_CODE_FILENAME] = (
        len(code),
        None,
        code.splitlines(keepends=True),
        AGENT_CODE_FILENAME,
    )


def _exc_type_name(te: TracebackException) -> str:
    name = getattr(te, "exc_type_str", None)
    if name is not None:
        return name
    return te.exc_type.__name__


def _filter_stack(stack: StackSummary) -> StackSummary:
    return StackSummary.from_list([f for f in stack if f.filename == AGENT_CODE_FILENAME])


def _format_level(te: TracebackException) -> str:
    if _exc_type_name(te) in _SYNTAX_EXC_NAMES:
        return _strip_caret_lines("".join(te.format_exception_only()))
    kept = _filter_stack(te.stack)
    parts: list[str] = []
    if kept:
        parts.append(_TB_HEADER)
        parts.extend(kept.format())
    parts.extend(te.format_exception_only())
    return _strip_caret_lines("".join(parts))


def _format_chain(te: TracebackException) -> str:
    parts: list[str] = []
    if te.__cause__ is not None:
        parts.append(_format_chain(te.__cause__))
        parts.append(_CAUSE_MESSAGE)
    elif te.__context__ is not None and not te.__suppress_context__:
        parts.append(_format_chain(te.__context__))
        parts.append(_CONTEXT_MESSAGE)
    parts.append(_format_level(te))
    return "".join(parts)


def format_agent_traceback(exc: BaseException) -> str:
    te = TracebackException.from_exception(exc)
    return _format_chain(te)


def format_full_traceback(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


__all__ = [
    "AGENT_CODE_FILENAME",
    "format_agent_traceback",
    "format_full_traceback",
    "register_agent_source",
]
