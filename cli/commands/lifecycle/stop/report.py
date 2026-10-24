# meiva:note
# How a stop ends: the failure report with its phase accounting, or the completion notes.

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

from base.deploy.lifecycle.status_journal import finish, status_path
from cli.commands.lifecycle.stop.force_outcome import force_incomplete_report
from cli.commands.lifecycle.stop.render import completion_notes, incomplete_report, phase_timing


# meiva:note
# Print the stop's real failure with its per-phase budget accounting and
# close the lifecycle journal when this stop owns it.
#
# The journal record carries the structured evidence too: the failing stage and the
# surviving-process inventory a `StopIncompleteError` collected, so the diagnosis survives the
# process that printed it (issue #2162). A failed stop is reported as it is: nothing is
# restarted behind the operator's back.
def report_incomplete(
    exc: BaseException,
    phases: list[tuple[str, float]],
    *,
    home: Path,
    owns_journal: bool,
    journal_opened: bool,
) -> None:
    journal_path = str(status_path(home)) if journal_opened else None
    print(incomplete_report(str(exc), phases, journal_path), file=sys.stderr)
    if owns_journal:
        extra: dict[str, object] = {"phases": phase_timing(phases)}
        stage = getattr(exc, "stage", None)
        survivors = getattr(exc, "survivors", None)
        if stage:
            extra["stage"] = stage
        if survivors:
            extra["survivors"] = survivors
        finish(home, 1, error=f"{type(exc).__name__}: {exc}", extra=extra)


# meiva:note
# Print why a force stop ended before its plan did. A force stop opens no lifecycle journal.
def report_force_incomplete(exc: BaseException) -> None:
    print(force_incomplete_report(str(exc)), file=sys.stderr)


# meiva:note
# The stop is done; `notes` (an escalated Postgres shutdown) and `clients` (the pooler's
# clients still connected at its stop) go into the journal.
def finish_stop(
    home: Path, *, owns_journal: bool, notes: Sequence[str], clients: Sequence[str]
) -> int:
    for line in completion_notes(notes):
        print(line, file=sys.stderr)
    if owns_journal:
        extra: dict[str, object] = {}
        if notes:
            extra["escalations"] = list(notes)
        if clients:
            extra["pooler_clients"] = list(clients)
        finish(home, 0, extra=extra or None)
    return 0
