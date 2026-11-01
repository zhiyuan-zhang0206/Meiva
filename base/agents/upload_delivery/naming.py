# meiva:note
# What a delivered upload is called and where it lands, as values.
#
# A person's file keeps the name it had (`quarterly report (final).pdf`, `screenshot.png`): only a
# character that cannot be part of one file name is replaced, a name beyond the filesystem
# byte budget is shortened at its stem so the suffix survives, and two files of one send that
# would collide get a counter (`name (1).ext`). The directory a send lands in is its send time
# at second precision, with a counter when another send of the same agent shares the second.
# Nothing here reads a clock, a path or the filesystem: the same inputs always name the same
# files, so the rules are asserted by value.
# meiva:link gateway/upload_delivery/docs/delivered-uploads/source-publication.meiva.okf.md

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection, Iterable
from datetime import datetime
from pathlib import PurePosixPath

__all__ = [
    "MAX_NAME_BYTES",
    "SEND_DIRECTORY_PATTERN",
    "clean_filename",
    "delivered_names",
    "format_size",
    "name_key",
    "send_directory",
    "upload_note",
]

# meiva:note
# Reserve the native create-only temporary name (`.<name>.<uuid>.tmp`, 38 bytes of overhead)
# inside the 255-byte filesystem limit.
MAX_NAME_BYTES = 217
_MAX_SUFFIX_BYTES = 32
_UNSAFE = re.compile(r"[\x00-\x1f\x7f]")
SEND_DIRECTORY_PATTERN = r"^[0-9]{8}-[0-9]{6}(?:-[0-9]+)?$"
_UNTITLED = "untitled"


def _bytes(text: str) -> int:
    return len(text.encode())


def _fit(stem: str, budget: int) -> str:
    return stem.encode()[: max(budget, 0)].decode(errors="ignore")


def _split(name: str) -> tuple[str, str]:
    suffix = PurePosixPath(name).suffix
    if _bytes(suffix) > _MAX_SUFFIX_BYTES:
        return name, ""
    return name[: len(name) - len(suffix)], suffix


def _bounded(stem: str, suffix: str, *, reserved: int = 0) -> str:
    return _fit(stem, MAX_NAME_BYTES - _bytes(suffix) - reserved) + suffix


# meiva:note
# The file name a browser-supplied name becomes: its last path component (a `/` or `\` is a
# directory separator, never part of the name), with each control character replaced by `_`,
# unicode and spaces kept. A name that is empty or only dots is `untitled`; one beyond the
# filesystem byte budget loses the end of its stem and keeps its suffix.
def clean_filename(raw: str) -> str:
    name = _UNSAFE.sub("_", raw.replace("\\", "/").rsplit("/", 1)[-1])
    if name.strip(" .") == "":
        return _UNTITLED
    if _bytes(name) <= MAX_NAME_BYTES:
        return name
    stem, suffix = _split(name)
    shortened = _bounded(stem, suffix)
    return shortened if shortened.strip(" .") else _UNTITLED


# meiva:note
# Two names are the same file on a case-insensitive, normalization-insensitive filesystem
# (the default on macOS), so a send compares them in that form.
def name_key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


# meiva:note
# The names a send's files take, in order: each cleaned name, with `name (1).ext`,
# `name (2).ext` for every later file whose name an earlier one of the send already has.
def delivered_names(raw_names: Iterable[str]) -> list[str]:
    taken: set[str] = set()
    names: list[str] = []
    for raw in raw_names:
        name = clean_filename(raw)
        counter = 0
        stem, suffix = _split(name)
        while name_key(name) in taken:
            counter += 1
            tag = f" ({counter})"
            name = _fit(stem, MAX_NAME_BYTES - _bytes(suffix) - _bytes(tag)) + tag + suffix
        taken.add(name_key(name))
        names.append(name)
    return names


# meiva:note
# The directory a send lands in: its send time as `YYYYMMDD-HHMMSS` in the time's own zone.
# When another send of the agent already holds the name, the next free `-2`, `-3`, ... one.
def send_directory(sent_at: datetime, taken: Collection[str]) -> str:
    base = sent_at.strftime("%Y%m%d-%H%M%S")
    if base not in taken:
        return base
    counter = 2
    while f"{base}-{counter}" in taken:
        counter += 1
    return f"{base}-{counter}"


# meiva:note
# A size for the note the agent reads: `812 B`, `1.5 KB`, `3.2 MB`.
def format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    value = float(size)
    for unit in ("KB", "MB", "GB"):
        value /= 1024
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
    return f"{value:.1f} GB"


# meiva:note
# The system note that rides a send's files to the agent: one line per file, with the
# absolute path the agent opens (`meiva.self.attach` shows it natively, `meiva.understand`
# reads it). The agent reads the note before the person's message.
def upload_note(machine: str, files: Iterable[tuple[str, str, int, str]]) -> str:
    lines = [
        f"- {name} ({content_type}, {format_size(size)}): {path}"
        for name, content_type, size, path in files
    ]
    return f"Files uploaded with the next message (on {machine}):\n" + "\n".join(lines)
