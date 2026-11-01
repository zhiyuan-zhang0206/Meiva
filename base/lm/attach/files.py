# meiva:note
# Runs the attachment packing against the filesystem: the plan (`plan_attachments`) says which
# observation it needs next, this module resolves, stats, reads and measures, and hands the
# answer back. Every file read of the packing lives here; a file that cannot be read becomes an
# observation, never an exception.

from __future__ import annotations

import stat
from pathlib import Path

from base.lm.attach.constants import ATTACH_MAX_FILES_PER_TURN
from base.lm.attach.packing import (
    AttachEntry,
    AttachmentPack,
    DimensionsObserved,
    DimensionsRequest,
    Observation,
    PackRequest,
    ReadObserved,
    ReadRequest,
    StatObserved,
    StatRequest,
    plan_attachments,
)
from base.lm.catalog import ModelCatalog

__all__ = ["image_dimensions", "pack_attachments"]


def pack_attachments(
    model: str,
    entries: list[AttachEntry],
    *,
    catalog: ModelCatalog,
    max_files_per_turn: int = ATTACH_MAX_FILES_PER_TURN,
) -> AttachmentPack | None:
    plan = plan_attachments(model, entries, catalog=catalog, max_files_per_turn=max_files_per_turn)
    try:
        request = next(plan)
        while True:
            request = plan.send(_observe(request))
    except StopIteration as finished:
        pack: AttachmentPack | None = finished.value
        return pack


def _observe(request: PackRequest) -> Observation:
    match request:
        case StatRequest(entry_path=entry_path):
            return _stat(entry_path)
        case DimensionsRequest(path=path):
            return DimensionsObserved(image_dimensions(Path(path)))
        case ReadRequest(path=path):
            return _read(Path(path))


def _stat(entry_path: str) -> StatObserved:
    try:
        path = Path(entry_path).resolve()
    except (OSError, ValueError):
        return StatObserved(outcome="invalid_path", path=entry_path)
    try:
        file_stat = path.stat()
    except FileNotFoundError:
        return StatObserved(outcome="missing", path=str(path))
    except OSError:
        return StatObserved(outcome="unstatable", path=str(path))
    return StatObserved(
        outcome="stat",
        path=str(path),
        is_regular=stat.S_ISREG(file_stat.st_mode),
        size=file_stat.st_size,
    )


# meiva:note
# Read only image metadata (`(width, height)` in pixels), or None when the file cannot be opened as
# an image; attach packing and `meiva.understand` both read image dimensions here.
def image_dimensions(path: Path) -> tuple[int, int] | None:
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(path) as image:
            return image.size
    except (Image.DecompressionBombError, OSError, UnidentifiedImageError, ValueError):
        return None


def _read(path: Path) -> ReadObserved:
    try:
        return ReadObserved(path.read_bytes())
    except OSError:
        return ReadObserved(None)
