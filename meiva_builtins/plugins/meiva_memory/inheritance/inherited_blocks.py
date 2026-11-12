# meiva:note
# The inherited-memory decisions as values: which birth-chain rows this host can read, how each
# ancestor's `memory/INHERIT.md` body is clipped to the two size caps, and the note text that
# results. Warnings travel back as strings for the caller to log.
# meiva:link meiva_builtins/plugins/meiva_memory/docs/memory-inheritance.md
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from base.packages.text_budget import Unit, measure

_FRONTMATTER_OPEN = "---\n"

_FRAMING = (
    "Memory your ancestors chose to share with descendants (depth {depth}, "
    "nearest ancestor first). Use it as remembered guidance; verify "
    "facts that conflict with current evidence."
)
_BLOCK_TRUNCATION = "[truncated at {cap} chars — full entry: {path}]"
_TOTAL_TRUNCATION = (
    "[truncated: the {budget}-char inherited-memory budget is reached — "
    "further blocks omitted; read the ancestors' personal memory folders directly]"
)
_REMOTE_FOOTER = "Ancestors on other machines (not readable from here): {rows}."


@dataclass(frozen=True)
class Ancestor:
    agent_id: int
    label: str | None
    machine: str | None


@dataclass(frozen=True)
class AncestorSplit:
    local: tuple[Ancestor, ...]
    remote: tuple[Ancestor, ...]


@dataclass(frozen=True)
class EntryBlocks:
    ancestor: Ancestor
    entry_path: str
    blocks: tuple[str, ...]


@dataclass(frozen=True)
class SizeCaps:
    block_chars: int
    total_chars: int


@dataclass(frozen=True)
class InheritedContent:
    text: str
    block_count: int
    section_count: int
    warnings: tuple[str, ...]


# meiva:note
# The first `depth` rows of the birth chain, nearest first: those stored on this machine can be
# read, the others are named in the footer.
def split_ancestors(chain: Sequence[Ancestor], *, depth: int, this_machine: str) -> AncestorSplit:
    nearest = chain[:depth]
    return AncestorSplit(
        local=tuple(row for row in nearest if row.machine == this_machine),
        remote=tuple(row for row in nearest if row.machine != this_machine),
    )


def _frontmatter_body(content: str) -> str:
    if not content.startswith(_FRONTMATTER_OPEN):
        return content
    parts = content.split(_FRONTMATTER_OPEN, 2)
    return parts[2] if len(parts) >= 3 else content


# meiva:note
# The body of an ancestor's `memory/INHERIT.md`, with optional leading YAML frontmatter and
# surrounding whitespace stripped.
def extract_inherit_body(text: str) -> str:
    return _frontmatter_body(text.replace("\r\n", "\n")).strip()


def _clip_blocks(entry: EntryBlocks, cap: int) -> tuple[str, list[str]]:
    texts: list[str] = []
    warnings: list[str] = []
    for block in entry.blocks:
        chars = measure(block, Unit.CHARS)
        if cap > 0 and chars > cap:
            warnings.append(f"block in {entry.entry_path} is {chars} chars — truncated to {cap}")
            texts.append(
                f"{block[:cap].rstrip()}\n"
                f"{_BLOCK_TRUNCATION.format(cap=cap, path=entry.entry_path)}"
            )
        else:
            texts.append(block)
    return "\n\n".join(texts), warnings


def _section(entry: EntryBlocks, text: str) -> str:
    label = f" ({entry.ancestor.label})" if entry.ancestor.label else ""
    return f"## ancestor #{entry.ancestor.agent_id}{label} — {entry.entry_path}\n\n{text}"


# meiva:note
# The inherited-memory note text, or `None` when no ancestor declared a block.
#
# One section per entry, nearest ancestor first as given. The per-block cap clips first; the total
# budget then clips the crossing section to its remainder and omits the rest behind one marker.
# A cap of 0 disables that limit.
def render_inherited(
    entries: Sequence[EntryBlocks], remote: Sequence[Ancestor], *, depth: int, caps: SizeCaps
) -> InheritedContent | None:
    if not any(entry.blocks for entry in entries):
        return None
    warnings: list[str] = []
    sections: list[str] = []
    used = 0
    cutoff = False
    for entry in entries:
        if not entry.blocks:
            continue
        text, clipped = _clip_blocks(entry, caps.block_chars)
        warnings.extend(clipped)
        chars = measure(text, Unit.CHARS)
        if caps.total_chars > 0 and used + chars > caps.total_chars:
            remaining = max(caps.total_chars - used, 0)
            warnings.append(
                f"total budget of {caps.total_chars} chars reached at {entry.entry_path} — truncated"
            )
            if remaining > 0:
                sections.append(_section(entry, text[:remaining].rstrip()))
            cutoff = True
            break
        used += chars
        sections.append(_section(entry, text))
    body = "\n\n".join(sections)
    if cutoff:
        body += "\n\n" + _TOTAL_TRUNCATION.format(budget=caps.total_chars)
    if remote:
        rows = ", ".join(f"#{row.agent_id} ({row.machine or 'unknown machine'})" for row in remote)
        body += "\n\n---\n" + _REMOTE_FOOTER.format(rows=rows)
    return InheritedContent(
        text=f"{_FRAMING.format(depth=depth)}\n\n{body}",
        block_count=sum(len(entry.blocks) for entry in entries),
        section_count=len(sections),
        warnings=tuple(warnings),
    )
