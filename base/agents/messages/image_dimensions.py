# meiva:note
# Pure intrinsic image-dimension parsing and wire contracts for timeline media.

from __future__ import annotations

import base64
import struct
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from base.host.boundary.json_values import is_object_list

_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_JPEG_SOF_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


class ImageDimensions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    width: int = Field(gt=0)
    height: int = Field(gt=0)


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or not data.startswith(_PNG_SIG) or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return (width, height) if width > 0 and height > 0 else None


def _gif_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 10 or data[:6] not in (b"GIF87a", b"GIF89a"):
        return None
    width, height = struct.unpack("<HH", data[6:10])
    return (width, height) if width > 0 and height > 0 else None


def _positive_size(width: int, height: int) -> tuple[int, int] | None:
    return (width, height) if width > 0 and height > 0 else None


def _webp_vp8x(data: bytes) -> tuple[int, int] | None:
    if len(data) < 30:
        return None
    w = 1 + int.from_bytes(data[24:27], "little")
    h = 1 + int.from_bytes(data[27:30], "little")
    return _positive_size(w, h)


def _webp_vp8(data: bytes) -> tuple[int, int] | None:
    if len(data) < 30 or data[23:26] != b"\x9d\x01\x2a":
        return None
    w, h = struct.unpack("<HH", data[26:30])
    return _positive_size(w & 0x3FFF, h & 0x3FFF)


def _webp_vp8l(data: bytes) -> tuple[int, int] | None:
    if len(data) < 25 or data[20] != 0x2F:
        return None
    bits = int.from_bytes(data[21:25], "little")
    return _positive_size((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)


def _webp_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 25 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    chunk = data[12:16]
    if chunk == b"VP8X":
        return _webp_vp8x(data)
    if chunk == b"VP8 ":
        return _webp_vp8(data)
    if chunk == b"VP8L":
        return _webp_vp8l(data)
    return None


_EXIF_TRANSPOSE_ORIENTATIONS = frozenset({5, 6, 7, 8})
_EXIF_ORIENTATION_TAG = 0x0112


def _exif_orientation(segment: bytes) -> int | None:
    if len(segment) < 14 or not segment.startswith(b"Exif\x00\x00"):
        return None
    tiff = segment[6:]
    if tiff[:2] == b"II":
        byteorder = "little"
    elif tiff[:2] == b"MM":
        byteorder = "big"
    else:
        return None
    if len(tiff) < 8:
        return None
    ifd_offset = int.from_bytes(tiff[4:8], byteorder)
    if ifd_offset + 2 > len(tiff):
        return None
    entry_count = int.from_bytes(tiff[ifd_offset : ifd_offset + 2], byteorder)
    for idx in range(entry_count):
        base = ifd_offset + 2 + idx * 12
        if base + 12 > len(tiff):
            break
        tag = int.from_bytes(tiff[base : base + 2], byteorder)
        if tag == _EXIF_ORIENTATION_TAG:
            return int.from_bytes(tiff[base + 8 : base + 10], byteorder)
    return None


def _next_jpeg_marker(data: bytes, pos: int) -> tuple[int, int] | None:
    size = len(data)
    while pos < size and data[pos] != 0xFF:
        pos += 1
    while pos < size and data[pos] == 0xFF:
        pos += 1
    if pos >= size:
        return None
    return data[pos], pos + 1


def _sof_dimensions(data: bytes, pos: int, orientation: int | None) -> tuple[int, int] | None:
    height, width = struct.unpack(">HH", data[pos + 3 : pos + 7])
    if orientation in _EXIF_TRANSPOSE_ORIENTATIONS:
        width, height = height, width
    return _positive_size(width, height)


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    pos = 2
    size = len(data)
    orientation: int | None = None
    while (found := _next_jpeg_marker(data, pos)) is not None:
        marker, pos = found
        if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
            continue
        if pos + 2 > size:
            return None
        seg_len = struct.unpack(">H", data[pos : pos + 2])[0]
        if seg_len < 2 or pos + seg_len > size:
            return None
        if marker == 0xE1 and orientation is None:
            orientation = _exif_orientation(data[pos + 2 : pos + seg_len])
        elif marker in _JPEG_SOF_MARKERS and seg_len >= 7:
            return _sof_dimensions(data, pos, orientation)
        pos += seg_len
    return None


def image_dimensions_from_bytes(data: bytes) -> tuple[int, int] | None:
    return (
        _png_dimensions(data)
        or _jpeg_dimensions(data)
        or _gif_dimensions(data)
        or _webp_dimensions(data)
    )


def image_dimensions_from_data_uri(uri: str) -> tuple[int, int] | None:
    if not uri.startswith("data:image/"):
        return None
    header, sep, body = uri.partition(",")
    if not sep or ";base64" not in header.lower():
        return None
    try:
        data = base64.b64decode(body, validate=False)
    except (ValueError, TypeError):
        return None
    return image_dimensions_from_bytes(data)


def parse_image_dimensions_list(raw: object) -> list[ImageDimensions | None] | None:
    if not is_object_list(raw):
        return None
    dims: list[ImageDimensions | None] = []
    for entry in raw:
        if entry is None:
            dims.append(None)
            continue
        try:
            dims.append(ImageDimensions.model_validate(entry))
        except ValidationError:
            dims.append(None)
    if not dims or not any(entry is not None for entry in dims):
        return None
    return dims


def dimensions_for_data_uris(uris: Sequence[str] | None) -> list[ImageDimensions | None] | None:
    if not uris:
        return None
    dims: list[ImageDimensions | None] = []
    for uri in uris:
        measured = image_dimensions_from_data_uri(uri)
        dims.append(
            ImageDimensions(width=measured[0], height=measured[1]) if measured is not None else None
        )
    return dims if any(entry is not None for entry in dims) else None
