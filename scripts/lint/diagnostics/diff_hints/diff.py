# meiva:note
# Reads the unified diff (`git diff -U0 --no-renames`) into per-file changes. A path
# is the new path; a deleted file keeps its old path and `deleted=True`. The parse
# only trusts header lines before a file's first hunk: inside a hunk a removed line
# that starts with `-- ` is itself spelled `--- `, and must stay content.
from __future__ import annotations

import re

from scripts.lint.diagnostics.diff_hints.model import FileChange, RemovedLine

_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_FILE_HEADER = "diff --git "
_NULL = "/dev/null"


def _header_path(line: str, prefix: str) -> str | None:
    value = line[4:].split("\t", 1)[0].strip()
    if value == _NULL:
        return None
    return value[len(prefix) :] if value.startswith(prefix) else value


def _chunks(text: str) -> list[list[str]]:
    chunks: list[list[str]] = []
    for line in text.splitlines():
        if line.startswith(_FILE_HEADER):
            chunks.append([])
        if chunks:
            chunks[-1].append(line)
    return chunks


def _fallback_path(header: str) -> str:
    rest = header[len(_FILE_HEADER) :]
    _, separator, new = rest.partition(" b/")
    return new if separator else rest


def _parse_chunk(lines: list[str]) -> FileChange:
    old_path: str | None = None
    new_path: str | None = None
    added: set[int] = set()
    added_text: list[str] = []
    removed: list[RemovedLine] = []
    in_hunk = False
    old_line = new_line = 0
    for line in lines[1:]:
        hunk = _HUNK.match(line)
        if hunk:
            in_hunk = True
            old_line, new_line = int(hunk.group(1)), int(hunk.group(2))
        elif not in_hunk:
            if line.startswith("--- "):
                old_path = _header_path(line, "a/")
            elif line.startswith("+++ "):
                new_path = _header_path(line, "b/")
        elif line.startswith("+"):
            added.add(new_line)
            added_text.append(line[1:])
            new_line += 1
        elif line.startswith("-"):
            removed.append(RemovedLine(old_line, line[1:]))
            old_line += 1
        elif line.startswith(" "):
            old_line += 1
            new_line += 1
    path = new_path or old_path or _fallback_path(lines[0])
    deleted = new_path is None and old_path is not None
    return FileChange(path, frozenset(added), tuple(added_text), tuple(removed), deleted)


# meiva:note
# The changes of one diff, in diff order. An empty text is no change.
def parse_diff(text: str) -> tuple[FileChange, ...]:
    return tuple(_parse_chunk(chunk) for chunk in _chunks(text))
