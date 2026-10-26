# meiva:note
# The shell of a markdown note pool: walk a directory of `---`-frontmatter notes and read them.
#
# `base.packages.docs.notes` decides whether a text is a note and does no I/O; this is the one
# place that turns a pool directory into notes. A note is read whole, whatever its size.

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from loguru import logger

from base.packages.docs.notes import Note, parse_note

__all__ = ["walk_notes"]


# meiva:note
# Yield `(path, note)` for every valid note under `root`, in sorted order.
#
# Files whose name is in `skip_names` are skipped; an unreadable file is skipped with a
# `cannot read <rel>` warning (appended to `warnings` when given, else logged at WARNING);
# a file without valid frontmatter is skipped silently. `note.rel` is the
# posix path relative to `root` sans `suffix`.
def walk_notes(
    root: Path,
    *,
    suffix: str = ".md",
    skip_names: frozenset[str] = frozenset(),
    warnings: list[str] | None = None,
) -> Iterator[tuple[Path, Note]]:
    for md_file in sorted(root.rglob(f"*{suffix}")):
        if md_file.name in skip_names:
            continue
        rel = md_file.relative_to(root).as_posix()
        rel_id = rel[: -len(suffix)] if rel.endswith(suffix) else rel
        try:
            text = md_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            if warnings is not None:
                warnings.append(f"cannot read {rel}")
            else:
                logger.opt(exception=True).warning("cannot read note {}; skipped", rel)
            continue
        note = parse_note(text, rel_id)
        if note is None:
            continue
        yield md_file, note
