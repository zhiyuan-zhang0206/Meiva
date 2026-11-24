# meiva:link gateway/agents/docs/to-user-resources.md


from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import format_datetime

__all__ = [
    "CACHE_CONTROL",
    "FileValidators",
    "file_validators",
    "not_modified",
    "range_applies",
    "validator_headers",
]

CACHE_CONTROL = "private, no-cache"

_NS_PER_SECOND = 1_000_000_000


@dataclass(frozen=True, slots=True)
class FileValidators:
    etag: str
    last_modified: str


def file_validators(size: int, mtime_ns: int) -> FileValidators:
    return FileValidators(
        etag=f'"{size:x}-{mtime_ns:x}"',
        last_modified=format_datetime(
            datetime.fromtimestamp(mtime_ns // _NS_PER_SECOND, tz=UTC), usegmt=True
        ),
    )


def validator_headers(validators: FileValidators) -> dict[str, str]:
    return {"ETag": validators.etag, "Last-Modified": validators.last_modified}


def not_modified(validators: FileValidators, if_none_match: str | None) -> bool:
    if if_none_match is None:
        return False
    offered = [part.strip().removeprefix("W/") for part in if_none_match.split(",")]
    return "*" in offered or validators.etag in offered


def range_applies(validators: FileValidators, if_range: str | None) -> bool:
    return if_range is None or if_range.strip() == validators.etag
