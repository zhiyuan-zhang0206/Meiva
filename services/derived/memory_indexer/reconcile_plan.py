# meiva:note
# The decisions of the backend reconciliation tool: whether a write confirmation counts, why a
# backend connection failed, whether the two backends hold the same rows, and what a query's
# two orderings say. Values in, text or a verdict out; `memory_search_reconcile` reads the
# terminal, connects the backends and prints.

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Literal, assert_never

__all__ = [
    "WriteConfirmation",
    "confirm_write",
    "connection_refusal_lines",
    "ordering_lines",
    "refusal_message",
    "row_gap_lines",
    "summary_line",
]

WriteConfirmation = Literal["confirmed", "no_terminal", "eof", "not_exact"]


# meiva:note
# Whether `--allow-write` is confirmed. `interactive` is whether stdin is a terminal; `answer`
# is the line typed at the prompt, or None when the input ended (EOF). Only the exact word
# `yes` on a terminal confirms.
def confirm_write(*, interactive: bool, answer: str | None) -> WriteConfirmation:
    if not interactive:
        return "no_terminal"
    if answer is None:
        return "eof"
    if answer != "yes":
        return "not_exact"
    return "confirmed"


# meiva:note
# What the tool says (on stderr) when the confirmation did not hold; None when it did.
def refusal_message(verdict: WriteConfirmation) -> str | None:
    match verdict:
        case "confirmed":
            return None
        case "no_terminal":
            return "--allow-write requires an interactive terminal; no backend connected"
        case "eof":
            return "--allow-write confirmation was unavailable (EOF); no backend connected"
        case "not_exact":
            return "--allow-write was not confirmed; no backend connected"
        case _:
            assert_never(verdict)


# meiva:note
# The stderr lines for a failed backend connection. A read-only refusal usually means the
# indexer daemon has not repaired the backend yet; a writable failure is just reported.
def connection_refusal_lines(error: Exception, *, readonly: bool) -> tuple[str, ...]:
    if readonly:
        return (
            f"read-only backend connection refused: {error}",
            "The indexer daemon's next patrol (15 minutes by default) repairs the "
            "backend, or use --allow-write only if you own that backend.",
        )
    return (f"backend connection failed: {error}",)


# meiva:note
# The stderr lines when the two backends hold different path sets, or () when they agree. A
# stale or unsynced backend makes the ordering diff meaningless (a path missing on one side
# cannot be compared), so the tool reports the row-set gap and stops.
def row_gap_lines(
    name_a: str, name_b: str, paths_a: Collection[str], paths_b: Collection[str]
) -> tuple[str, ...]:
    set_a, set_b = set(paths_a), set(paths_b)
    if set_a == set_b:
        return ()
    only_a = sorted(set_a - set_b)
    only_b = sorted(set_b - set_a)
    return (
        f"row sets differ: {name_a}={len(set_a)} paths, {name_b}={len(set_b)}; "
        f"only in {name_a}: {only_a[:5]}",
        f"only in {name_b}: {only_b[:5]}",
        "sync the backends with the indexer daemon's next patrol and re-run",
    )


# meiva:note
# The stdout lines for one query whose two orderings differ, or () when they agree.
def ordering_lines(
    index: int,
    text: str,
    name_a: str,
    name_b: str,
    paths_a: Sequence[str],
    paths_b: Sequence[str],
) -> tuple[str, ...]:
    if list(paths_a) == list(paths_b):
        return ()
    return (
        f"[{index}] {text[:60]!r}",
        f"    {name_a:>10}: {list(paths_a)}",
        f"    {name_b:>10}: {list(paths_b)}",
    )


def summary_line(exact: int, total: int, name_a: str, name_b: str, k: int) -> str:
    return f"summary: {exact}/{total} queries identical ({name_a} vs {name_b}, k={k})"
