# meiva:note
# Shared markdown-note model — one parser for the whole repo.
#
# The memory pool (`gateway/routers/memory_api/memory.py`) and the OKF bundle graph
# (`base/packages/docs/okf_graph.py`) both build on `---`-frontmatter markdown notes; this
# module is the single place that decides a text is a note and maps frontmatter to the fields
# both consumers need. It does no I/O and stays schema-free: `Note` is plain data, and
# graph-specific semantics (primary_tag, edge filtering) live in the callers.

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeGuard

from base.packages.docs.frontmatter import parse_frontmatter_typed


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


@dataclass(frozen=True)
class Note:
    """One parsed markdown note from a note pool.

    `rel` is the posix path relative to the pool root, sans extension — the
    stable node id both graph consumers key on. `fm` keeps the raw frontmatter
    dict for callers that read fields this dataclass does not lift.
    """

    rel: str
    title: str
    description: str | None
    tags: tuple[str, ...]
    timestamp: str | None
    meiva_agent: str | None
    meiva_machine: str | None
    body: str
    fm: dict[str, Any]


def normalize_tags(raw: object) -> tuple[str, ...]:
    """Coerce a frontmatter `tags` value to a tuple of strings.

    A bare string is wrapped; a list keeps only its string elements; anything
    else (`None`, an int, a dict) is an empty tuple — tags are a string list
    and nothing else, so a malformed note degrades to untagged instead of
    crashing the caller (`tags: 5` used to TypeError the graph endpoint).
    """
    if isinstance(raw, str):
        return (raw,)
    if _is_object_list(raw):
        return tuple(t for t in raw if isinstance(t, str))
    return ()


def parse_note(text: str, rel: str) -> Note | None:
    """Parse one note's frontmatter into a `Note`; `None` when the text is not
    a note (no/invalid frontmatter) — the caller skips it silently."""
    parsed = parse_frontmatter_typed(text)
    if parsed is None:
        return None
    fm, body = parsed

    title = fm.get("title", rel)
    if not isinstance(title, str):
        title = str(title) if title is not None else rel

    description = fm.get("description")
    description = str(description) if description is not None else None

    return Note(
        rel=rel,
        title=title,
        description=description,
        tags=normalize_tags(fm.get("tags")),
        timestamp=str(fm["timestamp"]) if fm.get("timestamp") else None,
        meiva_agent=str(fm["meiva_agent"]) if fm.get("meiva_agent") else None,
        meiva_machine=str(fm["meiva_machine"]) if fm.get("meiva_machine") else None,
        body=body,
        fm=fm,
    )
