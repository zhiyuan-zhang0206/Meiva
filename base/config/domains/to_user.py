# meiva:note
# The read-side guards are `settings.to_user` fields, cluster-pinned and consumed by both the ops listener and the gateway. max_document_bytes (1 MiB) guards against reading the wrong file as a document, not against writing — the soft word limit shapes the panel. max_references (1024) caps the per-reference stat probes of a pathological document. max_chain_depth (16) stops runaway or circular reference chains. max_resource_bytes (64 MiB) bounds one served file and with it the gateway's per-request buffering. max_open_range_bytes (2 MiB) is the slice an open-ended `Range: bytes=N-` read is answered with, so a media element streams a file in pieces instead of one whole-file response. Every bound must be positive; the first-version values were 256 KiB / 256 / 8 / 20 MiB. file_access ("all") is how far the panel's file viewer reaches on the owning machine.
# meiva:link base/config/docs/to-user-limits.md
from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic.config import JsonDict

from base.config.base import EnvSettings

# meiva:note
# How far the panel's file viewer reads on the machine that owns a path: `all` any path the
# account can read, `workspaces` only agent workspaces and the agent's working directory.
FileAccess = Literal["all", "workspaces"]

_TO_USER_FIELD: JsonDict = {
    "restart_required": "all",
    "writable": True,
    "sensitive": False,
    "scope": "cluster-pinned",
}


class ToUserSettings(EnvSettings):
    max_document_bytes: int = Field(
        title="Panel document size limit (bytes)",
        default=1024 * 1024,
        gt=0,
        alias="MEIVA_TO_USER_MAX_DOCUMENT_BYTES",
        description=(
            "Largest Markdown panel document the reader accepts, in bytes. A guard "
            "against reading the wrong file as a panel document, not a writing "
            "bound: the 1500-word soft limit shapes the panel "
            "(`to_user_soft_limit_words`) and the largest document in the fleet is "
            "about 5 KiB. The first-version bound was 256 KiB; 1 MiB keeps even "
            "extreme reports readable."
        ),
        json_schema_extra=_TO_USER_FIELD,
    )

    max_references: int = Field(
        title="Maximum references per panel document",
        default=1024,
        gt=0,
        alias="MEIVA_TO_USER_MAX_REFERENCES",
        description=(
            "Most local references one panel document may carry. Each reference "
            "costs one stat/version probe while the document is read; the bound "
            "caps a pathological document rather than normal authoring — real "
            "panels carry a handful. The first-version bound was 256."
        ),
        json_schema_extra=_TO_USER_FIELD,
    )

    max_chain_depth: int = Field(
        title="Maximum nested document reference depth",
        default=16,
        gt=0,
        alias="MEIVA_TO_USER_MAX_CHAIN_DEPTH",
        description=(
            "Longest reference chain a resource read may follow, across nested "
            "Markdown documents. Real report chains nest two or three documents; "
            "the bound stops a runaway or circular chain. The first-version bound "
            "was 8."
        ),
        json_schema_extra=_TO_USER_FIELD,
    )

    max_resource_bytes: int = Field(
        title="Referenced file size limit (bytes)",
        default=64 * 1024 * 1024,
        gt=0,
        alias="MEIVA_TO_USER_MAX_RESOURCE_BYTES",
        description=(
            "Largest single referenced file served through the authenticated "
            "resource endpoint, in bytes. Both ends enforce it: the reading "
            "machine refuses larger files and the gateway aborts a larger stream "
            "with 413. Sized for evidence-grade attachments — logs and data files "
            "in workspaces already reach 30-40 MiB — and since the gateway buffers "
            "the body in memory, this is also the per-request memory bound. The "
            "first-version bound was 20 MiB."
        ),
        json_schema_extra=_TO_USER_FIELD,
    )

    max_open_range_bytes: int = Field(
        title="Open-ended Range slice size (bytes)",
        default=2 * 1024 * 1024,
        gt=0,
        alias="MEIVA_TO_USER_MAX_OPEN_RANGE_BYTES",
        description=(
            "Longest slice one open-ended byte-range read (`Range: bytes=N-`, no end) "
            "is answered with; the `206` carries a `Content-Range` that tells the client "
            "where the slice ends and the client asks for the next one. A media element "
            "opens every file with `bytes=0-` and abandons that request as soon as it "
            "has the metadata or the user seeks, so the slice bounds what an abandoned "
            "request wastes on the link (100 Mb/s between two machines is 12 MB/s: 2 MiB "
            "is about 170 ms of wire time, where a 20 MB file would hold the link for "
            "1.7 s). It is also what gives the file its pieces: 2 MiB is about 87 s of "
            "192 kb/s audio or 0.8 s of 20 Mb/s video, enough that the next request "
            "(one round trip, 15-80 ms measured between the machines) is issued well "
            "before the buffer drains. A range that names its end is answered in full up "
            "to `max_resource_bytes`; only the open end is shortened."
        ),
        json_schema_extra=_TO_USER_FIELD,
    )

    file_access: FileAccess = Field(
        title="File viewer reach",
        default="all",
        alias="MEIVA_TO_USER_FILE_ACCESS",
        description=(
            "Which paths the panel's file viewer may read, list and serve on the "
            "machine that owns them. `all`: any path the account can read (absolute, "
            "`~/...`, or relative to the agent's working directory or workspace). "
            "`workspaces`: only agent workspaces and the agent's working directory; "
            "anything else is refused with 403. Non-regular files and the size limits "
            "are refused in both modes. The gateway decides and tells the owning "
            "runner with each forwarded read."
        ),
        json_schema_extra=_TO_USER_FIELD,
    )
