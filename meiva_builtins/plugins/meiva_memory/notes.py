from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from langchain_core.messages import HumanMessage

from agent.messages import NoteTag, system_note_message
from base.agents.context import MeivaContext
from base.log import logger
from base.packages.text_budget import LengthBudget, Level, Unit, evaluate, measure
from base.paths import memory_dir, workspace_dir

_MEMORY_INDEX_FILE = "MEMORY.md"

# meiva:link meiva_builtins/plugins/meiva_memory/docs/memory-index-notes.md
_PER_AGENT_MEMORY_DIRNAME = "memory"


def personal_memory_dir(workspace: Path) -> Path:
    return workspace / _PER_AGENT_MEMORY_DIRNAME


# meiva:link meiva_builtins/plugins/meiva_memory/docs/memory-index-notes.md
_NO_CONTENT = "(no content)"
DEFAULT_PERSONAL_INDEX_MAX_LINES = 200


@dataclass(frozen=True, slots=True)
class PersonalMemoryIndex:
    body: str
    raw_text: str
    lines: int
    maintenance_suffix: str
    over_cap: bool


def read_shared_memory_index(shared: Path) -> str | None:
    path = shared / _MEMORY_INDEX_FILE
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning("[shared-memory] unreadable index {}: {}", path, exc)
        return None
    return text or None


# meiva:note
# Both index notes are also given to an external harness, which writes files with its own tools
# rather than `meiva.files`, so the framing names a folder, not a tool: an absolute path (with the
# SDK constant that names it) and "write a file there". The same text then holds for every harness.
# meiva:link meiva_builtins/plugins/meiva_memory/docs/memory-index-notes.md
def shared_index_framing(shared: Path) -> str:
    return (
        "The shared memory index — visible to every agent in the cluster (your personal "
        "memory is separate and yours alone). Below is MEMORY.md in the shared memory folder "
        f"{shared} (meiva.memory.SHARED_PATH): a Setup section carrying inline the few facts "
        "every agent needs, then Pointers, one line per note as "
        "`- [Title](path.md) — one-line description`. Shared memory is a plain folder of "
        "notes there — read a pointer, list/glob/grep to browse, or meiva.memory.search for "
        "a semantic lookup. A note's body lives in its own file, never in a pointer line. "
        f"Write a shared note as a Markdown file under {shared}; "
        "meiva.help(meiva.memory) shows the format."
    )


# meiva:link meiva_builtins/plugins/meiva_memory/docs/memory-index-notes.md
def memory_index_note(ctx: MeivaContext) -> HumanMessage | None:
    slices = ctx.require_agent()
    if slices.sandbox.eval_isolation or not slices.read("agent", "memory_index_inject_enabled"):
        return None
    shared = memory_dir(home=ctx.require_meiva_home())
    text = read_shared_memory_index(shared)
    if text is None:
        return None
    now = ctx.require_clock().now()
    return system_note_message(
        content=f"{shared_index_framing(shared)}\n\n{text}",
        tag=NoteTag.MEMORY,
        picked_up_at=now,
        created_at=now,
        inputs=ctx.timestamp_policy(),
    )


# meiva:link meiva_builtins/plugins/meiva_memory/docs/memory-index-notes.md
def personal_index_framing(personal: Path, shared: Path) -> str:
    return (
        "Your personal memory index. Where a compact summary records what happened "
        "in one conversation round, this persists across compactions and sessions. Personal "
        "memory is yours alone; the shared memory that every agent reads is separate "
        f"({shared}, meiva.memory.SHARED_PATH).\n\n"
        f"Below is MEMORY.md in your personal memory folder {personal} "
        "(meiva.memory.PERSONAL_PATH), your index of personal entry files. "
        "MEMORY.md and the entries `<short-kebab-slug>.md` beside it hold your own personal "
        "memory (tied to your own identity and task state, never inherited by descendants). "
        f"Write a personal entry as a pure Markdown file in {personal}; "
        "meiva.help(meiva.memory) shows the format. You keep the "
        "index yourself: one line per memory, `- [Title](<slug>.md) - description`, and never "
        f"entry content. Read an entry on demand from `{personal}/<slug>.md`. "
        "Domain context and execution rules that your spawned descendants should inherit "
        f"belong in `{personal}/INHERIT.md` (plus optional detail files under "
        f"`{personal}/inherit/<slug>.md`), written directly in pure Markdown — descendants "
        "automatically receive INHERIT.md at startup. Detailed task notes, logs, and "
        "artifacts stay in workspace files; facts other agents need go to shared memory "
        f"({shared})."
    )


# meiva:link meiva_builtins/plugins/meiva_memory/docs/memory-index-notes.md
_OVER_CAP_NOTE = (
    "\n\n---\n[maintenance] This index is {lines} lines, over the "
    "{cap}-line soft cap — move detail into entry files and prune stale "
    "pointers to keep it lean."
)


# meiva:link meiva_builtins/plugins/meiva_memory/docs/memory-index-notes.md
def _per_agent_maintenance_suffix(text: str, *, cap: int) -> str:
    verdict = evaluate(text, LengthBudget(Unit.LINES, soft=max(cap, 0)))
    if verdict.level is Level.OK:
        return ""
    return _OVER_CAP_NOTE.format(lines=verdict.measured, cap=cap)


def read_personal_memory_index(
    mem_dir: Path,
    *,
    max_lines: int = DEFAULT_PERSONAL_INDEX_MAX_LINES,
    agent_id: int | None = None,
) -> PersonalMemoryIndex:
    path = mem_dir / _MEMORY_INDEX_FILE
    try:
        mem_dir.mkdir(parents=True, exist_ok=True)
        if not path.is_file():
            logger.info(
                "[per-agent-memory] agent={} path={} — creating empty index", agent_id, path
            )
            path.write_text("", encoding="utf-8")
    except OSError as exc:
        logger.warning("[per-agent-memory] unable to initialize {}: {}", path, exc)
    text = ""
    if path.is_file():
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning("[per-agent-memory] unreadable index {}: {}", path, exc)
    body = text if text else _NO_CONTENT
    lines = measure(text, Unit.LINES)
    suffix = _per_agent_maintenance_suffix(text, cap=max_lines)
    return PersonalMemoryIndex(
        body=body,
        raw_text=text,
        lines=lines,
        maintenance_suffix=suffix,
        over_cap=bool(suffix),
    )


# meiva:link meiva_builtins/plugins/meiva_memory/docs/memory-index-notes.md
def per_agent_memory_note(ctx: MeivaContext) -> HumanMessage | None:
    if not ctx.require_agent().read("agent", "memory_per_agent_inject_enabled"):
        logger.debug("[per-agent-memory] disabled by settings")
        return None
    from meiva.sdk_surface.agent_identity import agent_id

    aid = agent_id(ctx)
    if aid is None:
        logger.debug("[per-agent-memory] agent id not yet established, skipping")
        return None
    ws = workspace_dir(aid, home=ctx.require_meiva_home())
    mem_dir = personal_memory_dir(ws)
    index = read_personal_memory_index(
        mem_dir,
        max_lines=ctx.require_agent().read("agent", "memory_per_agent_index_max_lines"),
        agent_id=aid,
    )
    logger.info(
        "[per-agent-memory] agent={} path={} lines={} {} over_cap={}",
        aid,
        mem_dir / _MEMORY_INDEX_FILE,
        index.lines,
        "injected" if index.raw_text else "empty",
        index.over_cap,
    )
    now = ctx.require_clock().now()
    return system_note_message(
        content=f"{personal_index_framing(mem_dir, memory_dir(home=ctx.require_meiva_home()))}"
        f"\n\n{index.body}{index.maintenance_suffix}",
        tag=NoteTag.AGENT_MEMORY,
        picked_up_at=now,
        created_at=now,
        inputs=ctx.timestamp_policy(),
    )
