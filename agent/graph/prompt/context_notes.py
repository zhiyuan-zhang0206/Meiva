# meiva:note
# Framework and plugin notes are stably sorted by cache rank, placing stable settings before changing memory indexes. Agent identity and concrete workspace paths live in `on_fork` notes, not the shared system prompt; a fork keeps the inherited copy byte-identical (the prefix cache it rides) and appends its own after the marker, which says the notes above are the source's. The timezone IANA name remains authoritative when an offset becomes stale across DST. Fork skill additions append at the tail to preserve the inherited prefix.
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from langchain_core.messages import HumanMessage

from agent.messages import NoteTag, system_note_message
from base.agents.context import MeivaContext
from base.agents.to_user import read_document
from base.api_contracts.agent_contract import Harness
from base.log import logger
from base.packages.plugins.extensions import ContextNote, ExtensionRegistry
from base.packages.skills.tree import Skill, identifier, target
from base.paths import workspace_dir

from .live import effective_sdk_disable, read_skills, read_view, warn_unresolved
from .note_text import (
    Workspace,
    agent_id_text,
    exec_timeout_text,
    label_from_row,
    machine_name_text,
    timezone_text,
    workspace_display,
)
from .skill_selection import SkillView, resolve_prompt_skills

RANK_EXEC_TIMEOUT = 10
RANK_TIMEZONE = 15
RANK_CLUSTER_MEMORY = 20
RANK_AGENT_ID = 30
RANK_PER_AGENT_MEMORY = 40
RANK_INHERITED_MEMORY = 45
RANK_PRELOADED_SKILLS = 50


def _ordered(extensions: ExtensionRegistry, *, fork_only: bool) -> list[ContextNote]:

    plugin_notes = [note for _plugin, note in extensions.context_notes()]
    return [n for n in (*FRAMEWORK_NOTES, *plugin_notes) if n.on_fork or not fork_only]


def context_notes(extensions: ExtensionRegistry, ctx: MeivaContext) -> list[HumanMessage]:

    return _rendered(_ordered(extensions, fork_only=False), ctx)


def fork_notes(extensions: ExtensionRegistry, ctx: MeivaContext) -> list[HumanMessage]:

    return _rendered(_ordered(extensions, fork_only=True), ctx)


def _rendered(entries: list[ContextNote], ctx: MeivaContext) -> list[HumanMessage]:
    built: list[tuple[int, HumanMessage]] = []
    for entry in entries:
        note = entry.build(ctx)
        if note is None:
            continue
        if not isinstance(note, HumanMessage):
            raise TypeError(
                f"context note {entry.build.__name__} returned {type(note).__name__}, "
                "not HumanMessage | None"
            )
        built.append((entry.rank, note))
    return [note for _, note in sorted(built, key=lambda pair: pair[0])]


def _established_agent_id(ctx: MeivaContext, note: str) -> int | None:

    from meiva.sdk_surface.agent_identity import agent_id

    aid = agent_id(ctx)
    if aid is None:
        logger.debug("[context-notes] {} note skipped: no agent identity established", note)
    return aid


def exec_timeout_note(ctx: MeivaContext) -> HumanMessage | None:

    if ctx.harness is not Harness.MEIVA:
        return None
    if _established_agent_id(ctx, "exec-timeout") is None:
        return None
    return system_note_message(
        content=exec_timeout_text(ctx.require_agent().read("sandbox", "exec_timeout_seconds")),
        tag=NoteTag.EXEC_TIMEOUT,
        created_at=ctx.require_clock().now(),
    )


def timezone_note(ctx: MeivaContext) -> HumanMessage | None:

    if _established_agent_id(ctx, "timezone") is None:
        return None
    clock = ctx.require_clock()
    now = clock.now()
    return system_note_message(
        content=timezone_text(clock.timezone, now.astimezone(clock.explicit_zone())),
        tag=NoteTag.TIMEZONE,
        created_at=now,
    )


def own_label(ctx: MeivaContext, agent_id: int) -> str | None:

    with ctx.sql.cursor() as cur:
        cur.execute("SELECT label FROM agents WHERE id=%s", (agent_id,))
        return label_from_row(cur.fetchone())


def machine_clause(ctx: MeivaContext) -> str | None:

    from base.cluster.machine import MachineNameMissing

    if ctx.machine is None:
        return None
    try:
        name = ctx.machine.name()
    except MachineNameMissing:
        return None
    return machine_name_text(name)


def workspace_facts(workspace: Path, home: Path) -> Workspace:

    display = workspace_display(workspace, home)
    try:
        document = read_document(workspace)
    except (OSError, ValueError) as exc:
        return Workspace(display, None, str(exc))
    return Workspace(display, document.content, None)


def agent_id_note(ctx: MeivaContext) -> HumanMessage | None:

    aid = _established_agent_id(ctx, "agent-id")
    if aid is None:
        return None
    label = own_label(ctx, aid)
    machine = machine_clause(ctx)
    workspace = (
        workspace_facts(workspace_dir(aid, home=ctx.require_meiva_home()), ctx.require_user_home())
        if ctx.require_agent().read("agent", "workspace_in_system_prompt")
        else None
    )
    return system_note_message(
        content=agent_id_text(aid, label=label, machine=machine, workspace=workspace),
        tag=NoteTag.AGENT_ID,
        created_at=ctx.require_clock().now(),
    )


_PRELOADED_SKILLS_FRAMING = (
    "Preloaded skills — each section below contains a skill's complete SKILL.md. "
    "Follow these instructions as standing guidance; you do not need to reload "
    "these bodies. Read any supporting files referenced by a skill with "
    "meiva.help(meiva.skills.<path>) or meiva.files.read."
)

_FORK_TAIL_SKILLS_FRAMING = (
    "Additional skills for you — each section below contains a skill's complete "
    "SKILL.md. Follow these instructions as standing guidance alongside the "
    "skills already in your context; you do not need to reload these bodies."
)


def _render_skill_bodies(
    skills: Sequence[Skill], *, label: str
) -> tuple[list[str], list[str]] | None:

    sections: list[str] = []
    injected: list[str] = []
    for skill in skills:
        skill_md = Path(skill["path"]) / "SKILL.md"
        try:
            body = skill_md.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning(
                "[{}] skill {} SKILL.md unreadable ({}), skipping",
                label,
                skill["name"],
                exc,
            )
            continue
        sections.append(f"## meiva.skills.{target(skill)}\n\n{body}")
        injected.append(identifier(skill))
    if not sections:
        return None
    logger.info("[{}] injecting {} skill(s): {}", label, len(injected), ", ".join(injected))
    return sections, injected


def _skill_bodies_note(
    skills: Sequence[Skill],
    *,
    framing: str,
    label: str,
    created_at: datetime | None = None,
) -> HumanMessage | None:
    rendered = _render_skill_bodies(skills, label=label)
    if rendered is None:
        return None
    sections, _injected = rendered
    return system_note_message(
        content=framing + "\n\n" + "\n\n---\n\n".join(sections),
        tag=NoteTag.PRELOADED_SKILLS,
        created_at=created_at,
    )


def preloaded_note(
    skills: Sequence[Skill],
    *,
    harness: Harness,
    created_at: datetime | None = None,
) -> HumanMessage | None:

    framing = _PRELOADED_SKILLS_FRAMING
    if harness is not Harness.MEIVA:
        framing = framing.replace(
            "meiva.help(meiva.skills.<path>) or meiva.files.read.",
            "meiva.help(meiva.skills.<path>) or your native file-reading tool.",
        )
    return _skill_bodies_note(
        skills, framing=framing, label="preloaded-skills", created_at=created_at
    )


def preloaded_skills_note(ctx: MeivaContext) -> HumanMessage | None:

    prompt = ctx.require_agent().prompt
    view = read_view((), prompt.skills_to_expand_at_start, prompt.sdk_disable)
    selection = resolve_prompt_skills(view.expand, view)
    warn_unresolved("skills_to_expand_at_start", selection.unresolved)
    if not selection.skills:
        return None
    return preloaded_note(
        selection.skills, harness=ctx.harness, created_at=ctx.require_clock().now()
    )


def fork_tail_skills_note(
    names: list[str],
    sdk_disable: Sequence[str],
    *,
    created_at: datetime | None = None,
) -> HumanMessage | None:

    if not names:
        return None
    disabled = effective_sdk_disable(sdk_disable)
    selection = resolve_prompt_skills(names, SkillView((), (), disabled, read_skills(disabled)))
    warn_unresolved("fork_tail_skills", selection.unresolved)
    return _skill_bodies_note(
        selection.skills,
        framing=_FORK_TAIL_SKILLS_FRAMING,
        label="fork-tail-skills",
        created_at=created_at,
    )


FRAMEWORK_NOTES: tuple[ContextNote, ...] = (
    ContextNote(build=exec_timeout_note, rank=RANK_EXEC_TIMEOUT),
    ContextNote(build=timezone_note, rank=RANK_TIMEZONE),
    ContextNote(build=agent_id_note, on_fork=True, rank=RANK_AGENT_ID),
    ContextNote(build=preloaded_skills_note, on_fork=True, rank=RANK_PRELOADED_SKILLS),
)
