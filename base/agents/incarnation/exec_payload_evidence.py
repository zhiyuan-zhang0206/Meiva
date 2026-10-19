import hashlib
import json
import os
import stat
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

PayloadCondition = Literal["matched", "changed", "missing", "unreadable", "symlink", "oversize"]


@dataclass(frozen=True)
class PayloadObservation:
    condition: PayloadCondition
    authoritative: bool
    observed_sha256: str | None
    detail: str

    @property
    def matched(self) -> bool:
        return self.condition == "matched"

    @property
    def refused(self) -> bool:
        return self.authoritative and self.condition != "matched"


# meiva:note
# An identity that no longer derives raises here: the allocation no longer
# describes this incarnation, which no payload observation may excuse. Every
# payload condition - matched, changed, missing, unreadable, a symbolic link,
# oversize - leaves durable evidence and one anomaly event, and refuses only on a
# version 1 row.
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
    condition, detail = _classify(request_path)
    observed: str | None = None
    size_bytes: int | None = None
    if condition is None:
        try:
            content = read_owner_bytes(request_path, MAX_PAYLOAD_BYTES)
        except (OSError, ValueError) as error:
            condition, detail = "unreadable", f"{type(error).__name__}: {error}"
        else:
            observed = hashlib.sha256(content).hexdigest()
            size_bytes = len(content)
            if observed != allocation.recorded_payload_sha256:
                condition, detail = "changed", "payload bytes differ from the allocation"
    if condition is None:
        return PayloadObservation("matched", allocation.payload_is_authoritative, observed, "")
    evidence_path = _record_mismatch(
        allocation,
        request_path=request_path,
        agent_id=agent_id,
        generation=generation,
        runtime_owner=runtime_owner,
        phase=phase,
        condition=condition,
        detail=detail,
        observed=observed,
        size_bytes=size_bytes,
    )
    logger.error(
        "exec request payload {condition} ({detail}); refusing={refused} phase={phase}",
        event="exec_request_envelope_mutated",
        agent_id=agent_id,
        request=str(allocation.request),
        domain=str(allocation.domain),
        digest_version=allocation.digest_version,
        phase=phase,
        condition=condition,
        detail=detail,
        refused=allocation.payload_is_authoritative,
        recorded_payload_sha256=allocation.recorded_payload_sha256,
        observed_payload_sha256=observed,
        size_bytes=size_bytes,
        evidence_path=str(evidence_path) if evidence_path is not None else "",
    )
    return PayloadObservation(condition, allocation.payload_is_authoritative, observed, detail)


def evidence_directory(request_path: Path) -> Path:
    return request_path.parent.parent.parent / _EVIDENCE_DIRECTORY


# meiva:note
# Classify with lstat and never follow the entry: a link, a device or an oversized
# file is answered without opening it, so the evidence read cannot become a channel
# to another file or to an endless one.
def _classify(request_path: Path) -> tuple[PayloadCondition | None, str]:
    try:
        status = request_path.lstat()
    except FileNotFoundError:
        return "missing", "no request envelope"
    except OSError as error:
        return "unreadable", f"{type(error).__name__}: {error}"
    if stat.S_ISLNK(status.st_mode):
        return "symlink", "the entry is a symbolic link"
    if not stat.S_ISREG(status.st_mode):
        return "unreadable", "the entry is not a regular file"
    if status.st_size > MAX_PAYLOAD_BYTES:
        return "oversize", f"{status.st_size} bytes over the payload ceiling"
    return None, ""


def _record_mismatch(
    allocation: ExecAllocation,
    *,
    request_path: Path,
    agent_id: int,
    generation: UUID,
    runtime_owner: UUID,
    phase: str,
    condition: str,
    detail: str,
    observed: str | None,
    size_bytes: int | None,
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
        "condition": condition,
        "detail": detail,
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
