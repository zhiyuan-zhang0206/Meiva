"""Strict delivered-upload identity and versioned receiver proof."""

import hashlib
import json
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from base.agents.upload_delivery.naming import (
    MAX_NAME_BYTES,
    SEND_DIRECTORY_PATTERN,
    name_key,
)
from base.agents.upload_delivery.paths import MAX_UPLOAD_BYTES
from base.host.db_authority.unit import UnitIdentity

# meiva:link base/agents/upload_delivery/docs/storage.meiva.okf.md
PROTOCOL = 2
BATCH_ID_PATTERN = r"^[0-9a-f]{32}$"
FILE_ID_PATTERN = r"^[0-9a-f]{32}$"
NAME_PATTERN = r"^[^/\\\x00-\x1f\x7f]+$"
STATE = Literal["pending", "accepted", "hold"]


class UploadDeliveryConflictError(ValueError):
    """The immutable intent, object or frozen native placement no longer matches."""


class UploadQuotaExceededError(ValueError):
    """Native physical uploads and receiving reservations exhaust the quota."""


class Record(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class StagedFile(Record):
    file_id: str = Field(pattern=FILE_ID_PATTERN)
    name: str = Field(min_length=1, pattern=NAME_PATTERN)
    size: int = Field(ge=0, le=MAX_UPLOAD_BYTES)
    content_type: str = Field(min_length=1, max_length=255, pattern=r"^[\x20-\x7e]+$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class Object(Record):
    ordinal: int = Field(ge=0, lt=1000)
    name: str = Field(min_length=1, pattern=NAME_PATTERN)
    staged_id: str = Field(pattern=FILE_ID_PATTERN)
    size: int = Field(ge=0, le=MAX_UPLOAD_BYTES)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_type: str = Field(min_length=1, max_length=255, pattern=r"^[\x20-\x7e]+$")

    @model_validator(mode="after")
    def bounded_name(self) -> Self:
        if len(self.name.encode()) > MAX_NAME_BYTES or self.name.strip(" .") == "":
            raise ValueError("upload name is not a bounded file name")
        return self


class Manifest(Record):
    batch_id: str = Field(pattern=BATCH_ID_PATTERN)
    agent_id: int = Field(gt=0, lt=2**63)
    sent_at: str = Field(min_length=1, max_length=64)
    directory: str = Field(pattern=SEND_DIRECTORY_PATTERN)
    objects: list[Object] = Field(min_length=1, max_length=1000)

    @field_validator("sent_at")
    @classmethod
    def iso_time(cls, value: str) -> str:
        datetime.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def ordered_names(self) -> Self:
        for ordinal, item in enumerate(self.objects):
            if item.ordinal != ordinal:
                raise ValueError("manifest objects require ordered ordinals")
        if len({name_key(item.name) for item in self.objects}) != len(self.objects):
            raise ValueError("manifest object names must be unique")
        if len({item.staged_id for item in self.objects}) != len(self.objects):
            raise ValueError("manifest objects must be distinct staged files")
        return self

    def fingerprint(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class FrozenMessage(Record):
    text: str = Field(max_length=1_048_576)
    source: Literal["user"]


class VersionedRecord(Record):
    version: Literal[2]

    @field_validator("version", mode="before")
    @classmethod
    def explicit_integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("upload protocol version must be the explicit integer 2")
        return value


class ReceiveRequest(VersionedRecord):
    target: UnitIdentity
    source: UnitIdentity
    manifest: Manifest


class CopyProof(VersionedRecord):
    target: UnitIdentity
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    directory: str = Field(min_length=1)

    @field_validator("directory")
    @classmethod
    def absolute_directory(cls, value: str) -> str:
        if any(ord(c) < 32 for c in value) or not PurePosixPath(value).is_absolute():
            raise ValueError("copy proof requires an absolute native directory")
        return value


class ObjectReadRequest(Record):
    agent_id: int = Field(gt=0, lt=2**63)
    batch_id: str = Field(pattern=BATCH_ID_PATTERN)
    ordinal: int = Field(ge=0, lt=1000)


class Acceptance(Record):
    """Historical source acceptance; not copy readiness or agent execution."""

    batch_id: str
    agent_id: int
    directory: str
    files: list[Object]
    status_url: str


class DeliveryStatus(Record):
    batch_id: str
    state: STATE
    reason: str | None
    directory: str
    message_text: str
    files: list[Object]
    inbound_id: int | None
    source: UnitIdentity
    target: UnitIdentity
    attempts: int = Field(ge=0)
    next_attempt_at: datetime | None


def current_unit(
    *,
    local_machine: str,
    home: Path,
) -> UnitIdentity:
    return UnitIdentity(machine=local_machine, home=str(home.resolve()))


def request_hash(agent_id: int, file_ids: list[str], text: str) -> str:
    return hashlib.sha256(
        json.dumps([agent_id, file_ids, text], ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()


def staged_request_hash(name: str, size: int, sha256: str, content_type: str) -> str:
    return hashlib.sha256(
        json.dumps(
            [name, size, sha256, content_type], ensure_ascii=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
