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
from gateway.inspect._coverage import CoverageFacts, FamilyEvidence, coverage_notes
from gateway.inspect.schemas import InspectMetricsMetadata, MetricEvidence


def _family_evidence(evidence: MetricEvidence) -> FamilyEvidence:
    return FamilyEvidence(
        availability=evidence.availability,
        reason=evidence.reason,
        retained_unapplied_sources=tuple(evidence.retained_unapplied_sources),
    )


# meiva:note
# The part of one read's metadata the coverage verdicts read.
def coverage_facts(metadata: InspectMetricsMetadata) -> CoverageFacts:
    return CoverageFacts(
        window_start=metadata.window_start,
        collection_started_at=metadata.collection_started_at,
        cost=_family_evidence(metadata.cost),
        turns=_family_evidence(metadata.turns),
        activity=_family_evidence(metadata.activity),
        lifecycle=_family_evidence(metadata.lifecycle),
    )


def note_inspect_metrics_coverage(
    agent_id: int, metadata: InspectMetricsMetadata, *, spawned_at: datetime
) -> None:
    """Log every coverage limit of one read; the unexpected ones as the
    ``inspect_metrics_coverage_gap`` WARNING event. Never raises — see the
    module docstring."""
    try:
        for note in coverage_notes(coverage_facts(metadata), spawned_at=spawned_at):
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
