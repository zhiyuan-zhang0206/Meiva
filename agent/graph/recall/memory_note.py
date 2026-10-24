# meiva:note
# The decisions and wording of passive memory recall, over values. `qualifying` picks which
# search hits are injected: best score first, at or above the threshold, present on this
# machine (`present` is the set of hit paths whose file exists in the local pool, read by the
# caller), at most `limit`; a path not yet synced is skipped and takes no slot. `render_note_text`
# is the agent-visible note: each hit's pool-relative path, score and description, then the
# passage that matched unless it only repeats the description. Tags are left out, because the
# note is a pointer rather than a tag list. Which notes are injected is decided by score and
# bounded by the cap, because notes crowd the context they are meant to inform.
from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass

_FRAMING = (
    "Memory notes related to the current conversation, retrieved "
    "automatically, each with the passage that matched and its relevance score. "
    "These point into your note pool at meiva.memory.PATH -- read a path in full "
    "when it looks relevant."
)


# meiva:note
# One search hit as the injection decision reads it: the four fields it uses, copied by the
# caller from the gateway's search result so this module needs nothing from the gateway client.
@dataclass(frozen=True)
class SearchHit:
    path: str
    description: str
    score: float
    chunk: str


def qualifying(
    results: Sequence[SearchHit], *, threshold: float, limit: int, present: Collection[str]
) -> list[SearchHit]:
    picked: list[SearchHit] = []
    for item in sorted(results, key=lambda r: r.score, reverse=True):
        if item.score < threshold or len(picked) >= limit:
            break
        if item.path not in present:
            continue
        picked.append(item)
    return picked


def _render_entry(item: SearchHit) -> str:
    header = f"- {item.path} (score {item.score:.2f})"
    if item.description:
        header += f": {item.description}"
    if not item.chunk or item.chunk == item.description:
        return header
    passage = "\n".join(f"  > {line}" if line else "  >" for line in item.chunk.splitlines())
    return f"{header}\n{passage}"


def render_note_text(picked: Sequence[SearchHit]) -> str:
    return f"{_FRAMING}\n\n" + "\n".join(_render_entry(item) for item in picked)
