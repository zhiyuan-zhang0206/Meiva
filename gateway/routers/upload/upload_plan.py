# meiva:note
# What a silent upload batch is, decided from its bytes and names alone.
#
# `batches.py` owns the effects (the quota gate, the claim, the files, the receipt). The
# decisions that need none live here, so a test passes values and asserts what comes back:
#
# - the fingerprint that makes a retry of one key replay and a changed manifest conflict,
# - the immutable object names a batch gets, or the display suffix that cannot be one.

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

__all__ = [
    "RESERVED_PREFIX",
    "SuffixRefusal",
    "UploadItem",
    "batch_fingerprint",
    "plan_manifest",
]

RESERVED_PREFIX = "meiva-upload-"
_MAX_SUFFIX_BYTES = 128

type UploadItem = tuple[str, bytes, str]


# meiva:note
# The batch holds a display suffix that cannot be written into an immutable object URL.
@dataclass(frozen=True)
class SuffixRefusal:
    filename: str


# meiva:note
# The identity of what was uploaded: display name, size, digest and type, in order.
def batch_fingerprint(batch: Sequence[UploadItem], original_names: Sequence[str]) -> str:
    manifest = [
        [name, len(contents), hashlib.sha256(contents).hexdigest(), content_type]
        for name, (_, contents, content_type) in zip(original_names, batch, strict=True)
    ]
    canonical = json.dumps(manifest, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _representable(name: str) -> bool:
    suffix = PurePosixPath(name.replace("\\", "/")).suffix
    return not (
        "\x00" in name
        or len(suffix.encode()) > _MAX_SUFFIX_BYTES
        or any(char in "?#%" or ord(char) < 32 or ord(char) == 127 for char in suffix)
    )


# meiva:note
# The immutable objects of a batch, named by `batch_id` and position.
#
# A display name is metadata: only its suffix reaches the object name, and a suffix that
# would not survive a URL refuses the whole batch before anything is claimed.
def plan_manifest(
    batch: Sequence[UploadItem], original_names: Sequence[str], batch_id: str
) -> list[dict[str, object]] | SuffixRefusal:
    items: list[dict[str, object]] = []
    for ordinal, (name, (_, contents, content_type)) in enumerate(
        zip(original_names, batch, strict=True)
    ):
        if not _representable(name):
            return SuffixRefusal(name)
        suffix = PurePosixPath(name.replace("\\", "/")).suffix
        items.append(
            {
                "filename": name,
                "stored_name": f"{RESERVED_PREFIX}{batch_id}-{ordinal}{suffix}",
                "size": len(contents),
                "sha256": hashlib.sha256(contents).hexdigest(),
                "content_type": content_type,
            }
        )
    return items
