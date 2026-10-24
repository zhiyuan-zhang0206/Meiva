# meiva:note
# What an off-site publish decides, as values in and values out.
#
# `services.backup.artifact.offsite` owns the SDK, the credentials file and the
# upload. This module owns every judgement that needs none of them: which of the
# three destination keys are unset, the per-part integrity header, the multipart
# ETag chain, and whether the object the store reports is the one just uploaded.

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "META_PREFIX",
    "OssTarget",
    "chain_etag",
    "metadata_headers",
    "normalize_etag",
    "object_problem",
    "part_md5_header",
    "unset_keys",
]

META_PREFIX = "x-oss-meta-"


# meiva:note
# The configured destination: non-secret endpoint and bucket, secret file path.
@dataclass(frozen=True)
class OssTarget:
    endpoint: str
    bucket: str
    credentials_file: Path


# meiva:note
# The environment keys that name an unset destination field, in the order the
# operator reads them; empty when the destination is complete.
def unset_keys(endpoint: str, bucket: str, credentials_file: Path | None) -> tuple[str, ...]:
    keys = {
        "MEIVA_BACKUP_OFFSITE_ENDPOINT": endpoint,
        "MEIVA_BACKUP_OFFSITE_BUCKET": bucket,
        "MEIVA_BACKUP_OFFSITE_CREDENTIALS_FILE": credentials_file,
    }
    return tuple(key for key, value in keys.items() if not value)


# meiva:note
# The `Content-MD5` header value of one part: the base64 of its binary digest.
def part_md5_header(md5_hex: str) -> dict[str, str]:
    return {"Content-MD5": base64.b64encode(bytes.fromhex(md5_hex)).decode("ascii")}


# meiva:note
# The ETag OSS gives a multipart object: the MD5 of the parts' MD5s, a dash, and
# the part count.
def chain_etag(part_etags: Sequence[str]) -> str:
    digest = hashlib.md5("".join(part_etags).encode()).hexdigest()  # noqa: S324
    return f"{digest}-{len(part_etags)}"


# meiva:note
# Strip the HTTP quoting OSS puts around ETags; keep the server's case.
def normalize_etag(etag: str | None) -> str:
    return etag.strip().strip('"') if etag else ""


# meiva:note
# The user metadata as request headers (`x-oss-meta-<key>`).
def metadata_headers(metadata: Mapping[str, str]) -> dict[str, str]:
    return {f"{META_PREFIX}{key}": value for key, value in metadata.items()}


# meiva:note
# Why the object the store reports is not the one just uploaded, or None when it
# is: a missing property, another size, another ETag chain, or other metadata.
def object_problem(
    *,
    etag: str | None,
    content_length: int | None,
    headers: Mapping[str, str],
    size: int,
    part_etags: Sequence[str],
    metadata: Mapping[str, str],
) -> str | None:
    normalized = normalize_etag(etag)
    if not normalized or content_length is None:
        return "OSS object omitted its verification properties"
    stored = {
        key[len(META_PREFIX) :]: value
        for key, value in headers.items()
        if key.startswith(META_PREFIX)
    }
    if (
        int(content_length) != size
        or normalized.lower() != chain_etag(part_etags)
        or stored != dict(metadata)
    ):
        return "immutable OSS object differs from the local artifact"
    return None
