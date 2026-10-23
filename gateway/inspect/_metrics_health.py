# meiva:note
# Inspector metrics coverage verdicts — background log + signal (task #3869).
#
# The inspector panel stopped rendering the read model's coverage verdicts
# (user ruling 2026-09-17): "historical coverage is incomplete", a duration
# precision note, a retained-source note — machinery, not user copy. The
# backend keeps the record instead of the panel, one line per limit per read:
#
# - limits expected by construction log at DEBUG: a window reaching back before
#   the observation collection started (``historical_coverage_unknown``), a
#   legacy archive whose per-day precision cannot be attributed per turn
#   (``archive_precision_unattributed``, incl. retained sources). They are the
#   panel's chronic noise and carry no signal.
# - an UNEXPECTED limit — ``missing_turn_durations`` on a window inside the
#   collection era (turns recorded without their durations is a live recorder
#   gap, not history) — logs the ``inspect_metrics_coverage_gap`` WARNING event.
#   Every read that still shows the gap emits it again, so an alert rule reads
#   "still true" from the event stream and the episode ends when reads stop
#   showing it.
#
# Which limits a read shows is decided by `gateway.inspect._coverage`; this module logs them.
# Best-effort by contract: this runs on the statistics read path — nothing here
# may fail the read.

from __future__ import annotations

from datetime import datetime

from base.log import logger
from gateway.inspect._coverage import coverage_notes
from gateway.inspect.schemas import InspectMetricsMetadata


def note_inspect_metrics_coverage(
    agent_id: int, metadata: InspectMetricsMetadata, *, spawned_at: datetime
) -> None:
    """Log every coverage limit of one read; the unexpected ones as the
    ``inspect_metrics_coverage_gap`` WARNING event. Never raises — see the
    module docstring."""
    try:
        for note in coverage_notes(metadata, spawned_at=spawned_at):
            if note.unexpected:
                logger.warning(
                    "inspect metrics coverage gap",
                    event="inspect_metrics_coverage_gap",
                    agent_id=agent_id,
                    family=note.family,
                    condition=note.condition,
                    availability=note.availability,
                )
            else:
                logger.debug(
                    "inspect metrics coverage limit",
                    agent_id=agent_id,
                    family=note.family,
                    condition=note.condition,
                    availability=note.availability,
                )
    except Exception:
        logger.exception("inspect metrics coverage note failed", agent_id=agent_id)
