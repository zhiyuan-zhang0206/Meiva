"""Validated upload references shared by pending and impersonation timelines."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from base.host.boundary.json_values import is_object_dict, is_object_list

# meiva:note
# Suffix -> MIME for image uploads that can be inlined as native model content.
# Mirrors the image row of `meiva._understand`'s media map; a suffix not listed
# here is not treated as an inlineable image (the claim node never base64-inlines it).
IMAGE_MIME: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


class TimelineUploadFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    size: int = Field(ge=0)
    content_type: str = Field(min_length=1)
    url: str = Field(min_length=1)


def parse_upload_url(url: str) -> tuple[int, str] | None:
    """Split an upload url into (agent_id, name), or None if it is not the
    ``/api/agents/<int>/uploads/<name>`` shape. Does not check the file exists
    or that `name` is safe — callers pair this with `resolve_upload_path`."""
    prefix = "/api/agents/"
    if not url.startswith(prefix):
        return None
    rest = url[len(prefix) :]
    agent_part, sep, name = rest.partition("/uploads/")
    if not sep or not name or not agent_part.isdigit():
        return None
    return int(agent_part), name


# meiva:note
# MIME type for an image upload name, or None if the suffix/content_type is not an image.
def image_mime_for(name: str, content_type: str | None = None) -> str | None:
    mime = IMAGE_MIME.get(Path(name).suffix.lower())
    if mime is None or content_type is None:
        return mime
    media = content_type.split(";", 1)[0].strip().lower()
    return mime if media == mime else None


def _block_image_url(agent_id: int, raw_block: object) -> str | None:
    if not is_object_dict(raw_block) or raw_block.get("type") != "image_url":
        return None
    image_url = raw_block.get("image_url")
    url = image_url.get("url") if is_object_dict(image_url) else None
    if not isinstance(url, str):
        return None
    parsed = parse_upload_url(url)
    if parsed is None or parsed[0] != agent_id or image_mime_for(parsed[1]) is None:
        return None
    return url


def inbound_image_urls(agent_id: int, payload: Mapping[str, object] | None) -> list[str] | None:
    raw_images = payload.get("image_uploads") if payload else None
    if is_object_list(raw_images):
        urls = [
            f"/api/agents/{agent_id}/uploads/{item.batch_id}/{item.ordinal}"
            for raw in raw_images
            if (item := _validate_stored_file(raw)) is not None
            and image_mime_for(item.name, item.content_type) is not None
        ]
        if urls:
            return urls
    raw_blocks = payload.get("content_blocks") if payload else None
    if not is_object_list(raw_blocks):
        return None
    urls = [
        url
        for raw_block in raw_blocks
        if (url := _block_image_url(agent_id, raw_block)) is not None
    ]
    return urls or None


class _StoredUploadFile(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    name: str = Field(min_length=1)
    size: int = Field(ge=0)
    content_type: str = Field(min_length=1)
    batch_id: str = Field(min_length=32, max_length=32)
    ordinal: int = Field(ge=0)
    path: str | None = None


def _validate_stored_file(raw: object) -> _StoredUploadFile | None:
    try:
        return _StoredUploadFile.model_validate(raw)
    except ValidationError:
        return None


def _parse_upload_file(agent_id: int, raw: object) -> TimelineUploadFile | None:
    item = _validate_stored_file(raw)
    if item is None:
        return None
    return TimelineUploadFile(
        name=item.name,
        size=item.size,
        content_type=item.content_type,
        url=f"/api/agents/{agent_id}/uploads/{item.batch_id}/{item.ordinal}",
    )


def inbound_upload_files(
    agent_id: int, payload: Mapping[str, object] | None
) -> list[TimelineUploadFile] | None:
    raw_files = payload.get("upload_files") if payload else None
    if not is_object_list(raw_files):
        return None
    files = [
        parsed for raw in raw_files if (parsed := _parse_upload_file(agent_id, raw)) is not None
    ]
    return files or None


def kwargs_upload_files(raw: object) -> list[TimelineUploadFile] | None:
    if not is_object_list(raw):
        return None
    files: list[TimelineUploadFile] = []
    for item in raw:
        try:
            files.append(TimelineUploadFile.model_validate(item))
        except ValidationError:
            continue
    return files or None
