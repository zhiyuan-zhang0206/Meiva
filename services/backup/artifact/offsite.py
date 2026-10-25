# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false

"""Off-site publication of a finished daily dump to Aliyun OSS.

`publish` uploads one encrypted ``.dump.enc`` iff absent as ``<root>/<name>``
and logs the verified ACK; a missing or failed publish never costs the local
artifact. The only backend is OSS (``MEIVA_BACKUP_OFFSITE_ENDPOINT``,
``MEIVA_BACKUP_OFFSITE_BUCKET`` and ``MEIVA_BACKUP_OFFSITE_CREDENTIALS_FILE``); a home
that has not set all three skips the leg with one INFO line, because an
unconfigured home is a supported state.

The upload is a multipart upload whose integrity is proven twice: every part
carries a ``Content-MD5`` the server verifies, and the completed object's
ETag must equal the MD5-of-part-MD5s chain built from the server-returned
part ETags (the size and metadata must match too).

- iff-absent is server-enforced with ``x-oss-forbid-overwrite`` on
  CompleteMultipartUpload, NOT on InitiateMultipartUpload: init with the
  header on an occupied name fails at once (409 FileAlreadyExists), which
  would kill the adopt-after-crash retry before any part is streamed. A name
  that is already occupied at complete is adopted only when its size, ETag
  chain and metadata prove it the same content.
- Deployment constraint: a versioning-enabled (or suspended) bucket silently
  IGNORES ``x-oss-forbid-overwrite``, so iff-absent degrades to
  check-then-write. The bucket must stay versioning-off.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard

import oss2
from oss2.models import PartInfo

from services.backup.artifact.names import REMOTE_ROOT
from services.backup.artifact.offsite_plan import (
    OssTarget,
    metadata_headers,
    normalize_etag,
    object_problem,
    part_md5_header,
    unset_keys,
)

_log = logging.getLogger(__name__)

PART_SIZE = 32 * 1024 * 1024
"""Multipart part size: OSS parts (except the last) must be >= 100 KiB, and 32 MiB
keeps a 20 GB object at 640 parts."""

_FORBID_HEADER = {"x-oss-forbid-overwrite": "true"}
_METADATA = {"meiva-artifact-kind": "logical-backup"}
_CONNECT_TIMEOUT_S = 300


class OffsiteError(RuntimeError):
    """An off-site publish failed or found a different object under its name."""


@dataclass(frozen=True)
class OffsiteAck:
    """The store-verified identity of one published object."""

    object_name: str
    size: int
    pin_token: str
    md5: str
    created: bool


# meiva:note
# The configured OSS destination, or None (one INFO line names the unset keys).
def configured_target(
    endpoint: str, bucket: str, credentials_file: Path | None
) -> OssTarget | None:
    unset = unset_keys(endpoint, bucket, credentials_file)
    if unset or credentials_file is None:
        _log.info("[backup] off-site publish skipped: %s unset", ", ".join(unset))
        return None
    return OssTarget(endpoint, bucket, credentials_file)


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def _str_attr(value: object, name: str) -> str:
    raw = getattr(value, name)
    return raw if isinstance(raw, str) else str(raw)


def _int_attr(value: object, name: str) -> int:
    raw = getattr(value, name)
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise TypeError("expected int")
    return int(raw)


def _is_str_mapping(value: object) -> TypeGuard[Mapping[str, str]]:
    return isinstance(value, Mapping)


def _headers_attr(value: object, name: str) -> dict[str, str]:
    raw = getattr(value, name)
    if not _is_str_mapping(raw):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


# meiva:note
# Open the OSS bucket with the RAM AccessKey pair in the credentials file.
def open_bucket(target: OssTarget) -> oss2.Bucket:
    try:
        raw: object = json.loads(target.credentials_file.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError("OSS credentials file is not readable JSON") from exc
    if not _is_object_dict(raw):
        raise TypeError("OSS credentials payload must be an object")
    key_id, secret = raw.get("access_key_id"), raw.get("access_key_secret")
    if not isinstance(key_id, str) or not isinstance(secret, str) or not key_id or not secret:
        raise ValueError(
            "OSS credentials must carry a non-empty access_key_id and access_key_secret"
        )
    return oss2.Bucket(
        oss2.Auth(key_id, secret),
        target.endpoint.rstrip("/"),
        target.bucket,
        connect_timeout=_CONNECT_TIMEOUT_S,
    )


# meiva:note
# Best-effort publish of `artifact` as ``<root>/<name>``; the object name or None.
#
# An unconfigured destination (`target` is None) publishes nothing. An unusable
# credentials file or a failed upload logs the exception and retains the local
# artifact; neither raises, so the dump that produced `artifact` stays good.
def publish(artifact: Path, target: OssTarget | None, *, root: str = REMOTE_ROOT) -> str | None:
    if target is None:
        return None
    try:
        bucket = open_bucket(target)
    except Exception:
        _log.exception("[backup] off-site store unavailable; local artifact retained")
        return None
    object_name = f"{root}/{artifact.name}"
    try:
        ack = put_if_absent(bucket, artifact, object_name, _METADATA)
    except Exception:
        _log.exception(
            "[backup] off-site publish of %s failed; local artifact retained", object_name
        )
        return None
    _log.info(
        "[backup] off-site published %s (size=%d, pin=%s, checksum=md5:%s)",
        object_name,
        ack.size,
        ack.pin_token,
        ack.md5,
    )
    return object_name


# meiva:note
# Upload `path` as `object_name` iff absent, or adopt an identical object. The MD5 digests
# here are OSS's wire format for parts and objects, not secrets, so the S324 suppressions
# are the format's.
def put_if_absent(
    bucket: oss2.Bucket,
    path: Path,
    object_name: str,
    metadata: Mapping[str, str],
    *,
    part_size: int = PART_SIZE,
) -> OffsiteAck:
    size = path.stat().st_size
    if size <= 0:
        raise OffsiteError("off-site upload requires a non-empty artifact")
    upload_id: str | None = None
    part_etags: list[str] = []
    whole = hashlib.md5()  # noqa: S324
    try:
        # Deliberately NO forbid-overwrite on init: see the module docstring.
        raw_upload_id = getattr(
            bucket.init_multipart_upload(object_name, headers=metadata_headers(metadata)),
            "upload_id",
            None,
        )
        upload_id = raw_upload_id if isinstance(raw_upload_id, str) else None
        if not upload_id:
            raise OffsiteError("OSS multipart upload omitted its upload id")
        with path.open("rb") as source:
            while chunk := source.read(part_size):
                whole.update(chunk)
                part_etags.append(_upload_part(bucket, object_name, upload_id, chunk, part_etags))
        if not part_etags:
            raise OffsiteError("OSS upload produced no parts")
        parts = [PartInfo(number, etag) for number, etag in enumerate(part_etags, 1)]
        bucket.complete_multipart_upload(object_name, upload_id, parts, headers=_FORBID_HEADER)
        upload_id = None
        return _verified(
            bucket, object_name, size, whole.hexdigest(), part_etags, metadata, created=True
        )
    except oss2.exceptions.OssError as exc:
        if _is_file_exists(exc):
            return _verified(
                bucket, object_name, size, whole.hexdigest(), part_etags, metadata, created=False
            )
        raise OffsiteError(f"OSS upload failed: {exc}") from exc
    finally:
        if upload_id is not None:
            with suppress(oss2.exceptions.ServerError):
                bucket.abort_multipart_upload(object_name, upload_id)


# meiva:note
# One part, sent with the `Content-MD5` header the server verifies; its returned ETag must be
# the part's MD5. The digest is OSS's wire format, so the S324 suppression is the format's.
def _upload_part(
    bucket: oss2.Bucket, object_name: str, upload_id: str, data: bytes, uploaded: Sequence[str]
) -> str:
    digest = hashlib.md5(data).hexdigest()  # noqa: S324
    try:
        result = bucket.upload_part(
            object_name, upload_id, len(uploaded) + 1, data, headers=part_md5_header(digest)
        )
    except oss2.exceptions.OssError as exc:
        raise OffsiteError(f"OSS part upload failed: {exc}") from exc
    etag = normalize_etag(result.etag)
    if not etag or etag.lower() != digest:
        raise OffsiteError("OSS part ETag does not match its content MD5")
    return etag


def _extract_head(head: object) -> tuple[str, int, dict[str, str]]:
    return (
        _str_attr(head, "etag"),
        _int_attr(head, "content_length"),
        _headers_attr(head, "headers"),
    )


def _verified(
    bucket: oss2.Bucket,
    object_name: str,
    size: int,
    whole_md5: str,
    part_etags: Sequence[str],
    metadata: Mapping[str, str],
    *,
    created: bool,
) -> OffsiteAck:
    """The object under `object_name` proven to be exactly the uploaded parts.

    After a completion it must be our own upload; after FileAlreadyExists it
    is adopted only when it is the same content (a crashed or concurrent run
    of this publisher left it).
    """
    try:
        etag, content_length, headers = _extract_head(bucket.head_object(object_name))
    except oss2.exceptions.OssError as exc:
        raise OffsiteError(f"OSS verification failed: {exc}") from exc
    problem = object_problem(
        etag=etag,
        content_length=content_length,
        headers=headers,
        size=size,
        part_etags=part_etags,
        metadata=metadata,
    )
    if problem is not None:
        raise OffsiteError(problem)
    return OffsiteAck(object_name, size, normalize_etag(etag), whole_md5, created)


def _is_file_exists(exc: oss2.exceptions.OssError) -> bool:
    return isinstance(exc, oss2.exceptions.ServerError) and str(exc.code) == "FileAlreadyExists"
