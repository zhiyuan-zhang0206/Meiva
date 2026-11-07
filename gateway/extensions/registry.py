# meiva:note
# Write behind the CLI install; see gateway/extensions/docs/registry.meiva.okf.md.
from __future__ import annotations

import base64
import binascii
import json
import tarfile
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from fastapi import Path as PathParam
from pydantic import BaseModel, Field

from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of
from base.packages.extensions import registry
from base.packages.plugins.manifest import MANIFEST_FILENAME, ManifestError, validate
from base.packages.skills.names import match_key
from gateway.http.config import gateway_state

router = APIRouter()

# meiva:note
# Longest base64 text that can decode to an archive within the registry cap (4 characters per
# 3 bytes, padded), checked before decoding so an oversized body costs no decode and no scratch.
MAX_ARCHIVE_B64_CHARS = (registry.MAX_BLOB_BYTES + 2) // 3 * 4

_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"


class ExtensionRegisterRequest(BaseModel):
    source: str = Field(
        min_length=1,
        description="'repo', a git URL, or 'local:<machine>' — where the content came from.",
    )
    source_ref: str | None = Field(
        default=None, description="Commit or tag as installed, when the source is git."
    )
    archive_b64: str = Field(
        description="Base64 of the deterministic tar `registry.pack_tree` builds from the tree."
    )


class ExtensionRegisterResponse(BaseModel):
    name: str
    kind: registry.ExtensionKind
    content_hash: str = Field(description="Tree hash of the content the row now points at.")


def _plugin_manifest(root: Path, name: str) -> dict[str, object] | None:
    path = root / MANIFEST_FILENAME
    if not path.is_file():
        return None
    try:
        raw = object_of(json.loads, path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise HTTPException(status_code=422, detail=f"invalid plugin manifest: {exc}") from exc
    if not is_object_dict(raw):
        raise HTTPException(status_code=422, detail="plugin manifest must be a JSON object")
    try:
        parsed = validate(raw)
    except ManifestError as exc:
        raise HTTPException(status_code=422, detail=f"invalid plugin manifest: {exc}") from exc
    if match_key(parsed.name) != match_key(name):
        raise HTTPException(
            status_code=422,
            detail=f"plugin manifest name {parsed.name!r} does not match registry name {name!r}",
        )
    return raw


@router.put(
    "/api/extensions/{kind}/{name}",
    description="Store one extension's tree as a content-addressed blob and upsert its\nregistry row in one transaction. 413 over the registry size cap; 422 on an\nundecodable or unsafe archive.",
)
def put_extension(
    kind: registry.ExtensionKind,
    name: Annotated[str, PathParam(pattern=_NAME_PATTERN)],
    body: ExtensionRegisterRequest,
    request: Request,
) -> ExtensionRegisterResponse:
    if len(body.archive_b64) > MAX_ARCHIVE_B64_CHARS:
        raise HTTPException(
            status_code=413,
            detail=(
                f"extension {name!r} is over the {registry.MAX_BLOB_BYTES}-byte cluster "
                "registry cap. Remove the large files from the package, or install them by "
                "another route."
            ),
        )
    try:
        archive = base64.b64decode(body.archive_b64, validate=True)
    except binascii.Error as exc:
        raise HTTPException(status_code=422, detail=f"archive_b64 is not base64: {exc}") from exc
    with tempfile.TemporaryDirectory(prefix="meiva-extension-") as scratch:
        root = Path(scratch)
        try:
            registry.unpack_tree(archive, root)
        except (tarfile.TarError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=f"unusable archive: {exc}") from exc
        try:
            digest = registry.register_tree(
                gateway_state(request).db_pool,
                root=root,
                name=name,
                kind=kind,
                source=body.source,
                source_ref=body.source_ref,
                manifest=_plugin_manifest(root, name) if kind == "plugin" else None,
            )
        except registry.ExtensionTooLargeError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
    return ExtensionRegisterResponse(name=name, kind=kind, content_hash=digest)
