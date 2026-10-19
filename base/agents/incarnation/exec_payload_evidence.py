import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from base.agents.incarnation.exec_owner_protocol import MAX_PAYLOAD_BYTES, read_owner_bytes
from base.agents.incarnation.resources import (
    ExecAllocation,
    ResourceEvidenceError,
    exec_identity_digest,
)
from base.log import logger

_EVIDENCE_DIRECTORY = "evidence"
_RECORD_VERSION = 1


@dataclass(frozen=True)
class PayloadObservation:
    matched: bool
    authoritative: bool
    observed_sha256: str

    @property
    def refused(self) -> bool:
        return self.authoritative and not self.matched


# meiva:note
# An identity that no longer derives raises here: the allocation no longer
# describes this incarnation, which no payload observation may excuse. A payload
# mismatch always leaves durable evidence and one anomaly event, and refuses only
# on a version 1 row.
# meiva:link base/agents/incarnation/docs/incarnation-resources.meiva.okf.md
def observe_exec_payload(
    allocation: ExecAllocation,
    *,
    request_path: Path,
    agent_id: int,
    generation: UUID,
    runtime_owner: UUID,
    phase: Literal["settle", "recovery"],
) -> PayloadObservation:
    if allocation.digest_version == 2:
        identity = exec_identity_digest(
            request=allocation.request,
            domain=allocation.domain,
            agent_id=agent_id,
            generation=generation,
            runtime_owner=runtime_owner,
            deadline=allocation.deadline,
        )
        if identity != allocation.request_digest:
            raise ResourceEvidenceError("exec allocation identity differs from its recorded digest")
    content = read_owner_bytes(request_path, MAX_PAYLOAD_BYTES)
    observed = hashlib.sha256(content).hexdigest()
    matched = observed == allocation.recorded_payload_sha256
    if not matched:
        evidence_path = _record_mismatch(
            allocation,
            request_path=request_path,
            agent_id=agent_id,
            generation=generation,
            runtime_owner=runtime_owner,
            phase=phase,
            observed=observed,
            size_bytes=len(content),
        )
        logger.error(
            "exec request payload changed after allocation; refusing={refused} phase={phase}",
            event="exec_request_envelope_mutated",
            agent_id=agent_id,
            request=str(allocation.request),
            domain=str(allocation.domain),
            digest_version=allocation.digest_version,
            phase=phase,
            refused=allocation.payload_is_authoritative,
            recorded_payload_sha256=allocation.recorded_payload_sha256,
            observed_payload_sha256=observed,
            size_bytes=len(content),
            evidence_path=str(evidence_path) if evidence_path is not None else "",
        )
    return PayloadObservation(matched, allocation.payload_is_authoritative, observed)


def evidence_directory(request_path: Path) -> Path:
    return request_path.parent.parent.parent / _EVIDENCE_DIRECTORY


def _record_mismatch(
    allocation: ExecAllocation,
    *,
    request_path: Path,
    agent_id: int,
    generation: UUID,
    runtime_owner: UUID,
    phase: str,
    observed: str,
    size_bytes: int,
) -> Path | None:
    # meiva:note
    # A failed write must not swallow the observation, and a managed settlement
    # never removes the payload: the record points at it and carries both digests
    # instead of copying the file.
    record = {
        "version": _RECORD_VERSION,
        "request": str(allocation.request),
        "domain": str(allocation.domain),
        "agent_id": agent_id,
        "generation": str(generation),
        "runtime_owner": str(runtime_owner),
        "digest_version": allocation.digest_version,
        "expected_digest": allocation.request_digest,
        "recorded_payload_sha256": allocation.recorded_payload_sha256,
        "observed_payload_sha256": observed,
        "payload_path": str(request_path),
        "size_bytes": size_bytes,
        "observed_at": datetime.now(UTC).isoformat(),
        "phase": phase,
        "refused": allocation.payload_is_authoritative,
    }
    target = evidence_directory(request_path) / f"{allocation.request}.json"
    part = target.with_suffix(".json.tmp")
    try:
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        descriptor = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(record, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        part.replace(target)
    except OSError as error:
        part.unlink(missing_ok=True)
        logger.warning("exec payload evidence could not be written: {error}", error=str(error))
        return None
    return target
