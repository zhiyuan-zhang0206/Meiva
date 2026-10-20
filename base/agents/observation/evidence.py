"""Observation evidence, independent of lifecycle or execution progress."""

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel

# Existing machine-probe cadence and consecutive-failure contract.
LIVENESS_PASS_INTERVAL_S = 60.0
MACHINE_OFFLINE_AFTER_FAILURES = 2


# meiva:note
# Machine reachability is not proof of runtime ownership.
#
# This slice has no generation/owner binding, and a probe must not invent
# one. UI uses absolute deadlines.
class AgentObservation(BaseModel):
    machine_probe_at: datetime | None = None
    machine_probe_valid_until: datetime | None = None
    runtime_owner: Literal["unknown"] = "unknown"


def observation(machine_probe_at: datetime | None) -> AgentObservation:
    return AgentObservation(
        machine_probe_at=machine_probe_at,
        machine_probe_valid_until=(
            machine_probe_at
            + timedelta(seconds=LIVENESS_PASS_INTERVAL_S * MACHINE_OFFLINE_AFTER_FAILURES)
            if machine_probe_at is not None
            else None
        ),
    )


PROBE_FRESH_FOR = timedelta(seconds=LIVENESS_PASS_INTERVAL_S * MACHINE_OFFLINE_AFTER_FAILURES)
ADMISSION_FRESH_FOR = timedelta(minutes=5)


class AvailabilityReason(StrEnum):
    UNKNOWN = "unknown"
    HOST_UNAVAILABLE = "host_unavailable"
    AWAITING_ADMISSION = "awaiting_admission"
    ADMITTED = "admitted"
    LAUNCH_UNREACHABLE = "launch_unreachable"
    LAUNCH_REJECTED = "launch_rejected"
    LAUNCH_UNKNOWN = "launch_unknown"


class AgentAvailability(BaseModel):
    """Dispatch/admission evidence, never proof of first-message completion."""

    reason: AvailabilityReason
    observed_at: datetime
    evidence_at: datetime | None = None


def _fresh(stamp: datetime | None, at: datetime, max_age: timedelta) -> bool:
    return stamp is not None and timedelta(0) <= at - stamp <= max_age


def _launch_failure_availability(
    reason: str, at: datetime, evidence_at: datetime
) -> AgentAvailability:
    parsed = AvailabilityReason(reason)
    if parsed not in {
        AvailabilityReason.LAUNCH_UNREACHABLE,
        AvailabilityReason.LAUNCH_REJECTED,
        AvailabilityReason.LAUNCH_UNKNOWN,
    }:
        raise ValueError(f"invalid launch failure reason: {parsed}")
    return AgentAvailability(reason=parsed, observed_at=at, evidence_at=evidence_at)


def _validate_launch_failure_pair(
    launch_failure_reason: str | None, launch_failure_at: datetime | None
) -> None:
    if (launch_failure_reason is None) != (launch_failure_at is None):
        raise ValueError("launch failure reason and timestamp must be paired")


def _latest_evidence_at(
    probe_at: datetime | None, admission_at: datetime | None
) -> datetime | None:
    return max((stamp for stamp in (probe_at, admission_at) if stamp is not None), default=None)


# meiva:note
# Launch failure is durable; host/admission evidence expires by age.
#
# `host_online` is the existing pidfile verdict carried by status_probe, not
# proof that a turn can be admitted. The last admission is useful only while
# both it and the host probe are fresh.
def availability(
    *,
    status: str,
    host_online: bool | None,
    probe_at: datetime | None,
    admission_at: datetime | None,
    launch_failure_reason: str | None = None,
    launch_failure_at: datetime | None = None,
    now: datetime | None = None,
) -> AgentAvailability:
    at = now or datetime.now(UTC)
    _validate_launch_failure_pair(launch_failure_reason, launch_failure_at)
    if status != "terminated" and launch_failure_reason is not None:
        assert launch_failure_at is not None  # noqa: S101 — paired above
        return _launch_failure_availability(launch_failure_reason, at, launch_failure_at)
    latest_at = _latest_evidence_at(probe_at, admission_at)
    if status == "terminated" or host_online is None or not _fresh(probe_at, at, PROBE_FRESH_FOR):
        return AgentAvailability(
            reason=AvailabilityReason.UNKNOWN, observed_at=at, evidence_at=latest_at
        )
    if not host_online:
        return AgentAvailability(
            reason=AvailabilityReason.HOST_UNAVAILABLE, observed_at=at, evidence_at=probe_at
        )
    if admission_at is None:
        return AgentAvailability(
            reason=AvailabilityReason.AWAITING_ADMISSION, observed_at=at, evidence_at=probe_at
        )
    if not _fresh(admission_at, at, ADMISSION_FRESH_FOR):
        return AgentAvailability(
            reason=AvailabilityReason.UNKNOWN, observed_at=at, evidence_at=latest_at
        )
    return AgentAvailability(
        reason=AvailabilityReason.ADMITTED, observed_at=at, evidence_at=admission_at
    )
