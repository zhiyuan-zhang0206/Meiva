# meiva:note
# Complete per-incarnation exec allocation set, serialized by agents_meta.
#
# These are server-owned facts, never request payloads or configuration. The
# caller retains one explicit transaction through registration;
# no process launch, filesystem read or network wait belongs under that lock.

import hashlib
import json
import sys
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

import psutil
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from base import native_process
from base.native_process import ownership as proc_tree


class ResourceEvidenceError(RuntimeError):
    """Missing, stale or incomplete evidence cannot authorize resource work."""


class ResourceClosureError(ResourceEvidenceError):
    """A successor requires released ownership and closed exec resources.

    ``receipt_less`` distinguishes the two shapes: attached allocations whose
    terminal receipt never landed, versus an empty set whose runtime owner
    has not released it and whose host has not provably exited.
    """

    def __init__(self, message: str, *, receipt_less: bool) -> None:
        super().__init__(message)
        self.receipt_less = receipt_less


class ResourceFenceError(ResourceEvidenceError):
    """A force freeze or a host-identity change refuses same-owner admission."""


class ResourceShapeError(ResourceEvidenceError, ValueError):
    """A stored value the current model cannot decode: malformed, or written by
    a retired runtime (process receipts without native boot scope). No runtime
    path parses or replaces it."""


class _StrictEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ResourceBirth(_StrictEvidence):
    """Stamped only by a fresh metadata INSERT, not an empty-set assertion."""

    version: Literal[1] = 1
    state: Literal["unadmitted"] = "unadmitted"
    birth: UUID


class ResourceProcess(_StrictEvidence):
    pid: int = Field(gt=0)
    birth: float = Field(gt=0, allow_inf_nan=False)
    starttime: int | None = Field(gt=0)
    boot_id: str | None

    @field_validator("boot_id")
    @classmethod
    def canonical_boot(cls, value: str | None) -> str | None:
        if value is not None and str(UUID(value)) != value:
            raise ValueError("resource boot identity must be a canonical UUID")
        return value

    @classmethod
    def capture(cls, process: psutil.Process) -> "ResourceProcess":
        native = proc_tree.OwnedProcess.capture(process)
        evidence = cls(
            pid=native.pid,
            birth=native.birth,
            starttime=native.starttime,
            boot_id=native_process.native_boot_id(),
        )
        if not evidence.as_identity().live():
            raise ResourceEvidenceError("resource process ended during native capture")
        return evidence

    def _require_native_evidence(self) -> None:
        # Decode is platform-neutral: gateways also read remote runner rows.
        # Local process use requires every native field for this platform.
        match sys.platform:
            case "linux":
                valid = self.starttime is not None and self.boot_id is not None
            case "darwin":
                valid = self.starttime is None and self.boot_id is not None
            case _:
                valid = False
        if not valid:
            raise ResourceEvidenceError("resource process lacks this platform's native evidence")

    def in_current_boot(self) -> bool:
        self._require_native_evidence()
        return self.boot_id == native_process.native_boot_id()

    def as_identity(self) -> proc_tree.OwnedProcess:
        """Admit local native use only within the captured boot scope."""
        if not self.in_current_boot():
            raise ResourceEvidenceError("resource process belongs to another boot")
        return proc_tree.OwnedProcess(self.pid, self.birth, self.starttime)

    def same_birth(self, other: "ResourceProcess") -> bool:
        """Compare fresh captures without weakening immutable receipt equality."""
        self._require_native_evidence()
        other._require_native_evidence()
        return self.boot_id == other.boot_id and proc_tree.OwnedProcess(
            self.pid, self.birth, self.starttime
        ).same_birth(proc_tree.OwnedProcess(other.pid, other.birth, other.starttime))


# meiva:note
# Whether the exact recorded process is provably gone; unknown is never ended.
def process_ended(identity: ResourceProcess) -> bool:
    try:
        # meiva:note
        # Missing native evidence cannot become exit merely because a PID is
        # absent. Only a complete receipt can establish reuse or another boot.
        if not identity.in_current_boot():
            return True
        # meiva:note
        # PID reuse means the exact old process ended, not permission to signal
        # the replacement. This helper never signals or scans descendants.
        return not identity.as_identity().live()
    except (psutil.AccessDenied, OSError, RuntimeError, ValueError):
        return False


# meiva:note
# `digest_version` says what `request_digest` is the digest of: version 1 is the
# request envelope's bytes, version 2 the allocation's own identity, with the
# payload bytes riding separately in `request_sha256`. The envelope is readable by
# the code the allocation governs, so a version 2 allocation cannot let a payload
# write decide whether its closed domain is discharged.
# meiva:link base/agents/incarnation/docs/incarnation-resources.meiva.okf.md
class ExecAllocation(_StrictEvidence):
    request: UUID
    domain: UUID
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    deadline: datetime
    owner_process: ResourceProcess | None = None
    root_process: ResourceProcess | None = None
    digest_version: Literal[1, 2] = 1
    request_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def require_complete_identity(self) -> "ExecAllocation":
        if (self.owner_process is None) != (self.root_process is None):
            raise ValueError("exec owner/root identities must be attached together")
        if self.deadline.tzinfo is None:
            raise ValueError("exec deadline must be timezone-aware")
        return self

    @model_validator(mode="after")
    def require_versioned_payload_digest(self) -> "ExecAllocation":
        if self.digest_version == 2 and self.request_sha256 is None:
            raise ValueError("identity-digest allocation requires the payload digest")
        return self

    @property
    def payload_is_authoritative(self) -> bool:
        return self.digest_version == 1

    @property
    def recorded_payload_sha256(self) -> str:
        if self.digest_version == 1:
            return self.request_digest
        if self.request_sha256 is None:
            raise ResourceEvidenceError("identity-digest allocation lacks its payload digest")
        return self.request_sha256


# meiva:note
# Every field below is chosen by the launcher before the owner starts and is never
# rewritten by the exec'd code, which is what makes this digest fit to settle on.
# The canonical form is the UTF-8 lines `v1`, request hex, domain hex, agent id,
# generation, runtime owner and the deadline as ISO-8601 UTC microseconds, joined
# by newlines, hashed with sha256. That form is frozen: every stored version 2
# allocation re-derives it, so a change needs a new `digest_version`.
# meiva:link base/agents/incarnation/docs/incarnation-resources.meiva.okf.md
def exec_identity_digest(
    *,
    request: UUID,
    domain: UUID,
    agent_id: int,
    generation: UUID,
    runtime_owner: UUID,
    deadline: datetime,
) -> str:
    if deadline.tzinfo is None:
        raise ResourceEvidenceError("exec deadline must be timezone-aware")
    canonical = "\n".join(
        (
            "v1",
            request.hex,
            domain.hex,
            str(agent_id),
            str(generation),
            str(runtime_owner),
            deadline.astimezone(UTC).isoformat(),
        )
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class IncarnationResources(_StrictEvidence):
    version: Literal[1] = 1
    state: Literal["admitted"] = "admitted"
    generation: UUID
    owner: UUID
    host_process: ResourceProcess | None = None
    requests: dict[str, ExecAllocation]

    @model_validator(mode="after")
    def require_exact_keys(self) -> "IncarnationResources":
        if any(key != str(entry.request) for key, entry in self.requests.items()):
            raise ValueError("resource entry key differs from its exact request")
        return self


ResourceState = Annotated[ResourceBirth | IncarnationResources, Field(discriminator="state")]
_STATE: TypeAdapter[ResourceBirth | IncarnationResources] = TypeAdapter(ResourceState)


def decode_resources(value: object) -> ResourceBirth | IncarnationResources:
    if value is None:
        raise ResourceEvidenceError("incarnation resource set is unknown")
    try:
        return _STATE.validate_json(json.dumps(value))
    except ValidationError as exc:
        raise ResourceShapeError(
            "stored incarnation resources do not match the current model; "
            "explicit cutover reconciliation is required"
        ) from exc
