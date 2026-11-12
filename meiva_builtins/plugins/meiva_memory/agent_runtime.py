# meiva:note
# Agent-runtime face of the meiva_memory plugin: context notes, prompt section and recall hook.
#
# Loaded only in the agent process: the plugin loader (`base.packages.plugins.loading`) imports this
# module after `plugin.py` on the full path (host boot / graph build). The plugin's SDK surface, the
# `meiva.memory` namespace (PATH / PERSONAL_PATH / search), lives in `plugin.py` and loads in
# agent-launched children too (task #3633): exec code uses `meiva.memory` heavily, but never the
# context-note registry or the recall hook.

from __future__ import annotations

import asyncio
from collections.abc import Collection

from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from agent.graph.hooks.compact import auto_compact_will_fire
from agent.graph.prompt.context_notes import (
    RANK_CLUSTER_MEMORY,
    RANK_INHERITED_MEMORY,
    RANK_PER_AGENT_MEMORY,
)
from agent.graph.recall.memory_recall import PassiveRecall, passive_memory_recall
from agent.hooks import Hook
from agent.messages import tail_has_recallable_inbound
from agent.state import AgentState, MemoryState
from base.agents.context import MeivaContext
from base.api_contracts.agent_contract import Harness
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.lm.harness.handoff import EntryPoint, Handoff, HandoffSection, HarnessContribution
from base.log import logger
from base.packages.plugins.extensions import ContextNote, PluginContributions
from base.paths import memory_dir

from .inheritance.inherit import inherited_memory_note
from .notes import memory_index_note, per_agent_memory_note, personal_memory_dir

# The two memory indexes are declared in `contribute()` below, so `init_context` lays them down
# with the framework's notes whenever a window is established. The ranks pin them into the
# reading order the framework documents (see the rank scale in `context_notes.py`): the shared
# index right after the exec-timeout note, the per-agent index right after the agent-id note.
# Disabling this plugin means neither is ever declared, and a window is laid down with no memory
# notes in it — matching an agent that has no memory stores.


# ── Memory discipline (system prompt section) ──────────────────────────

# The write discipline both stores share. It is fixed text — the same bytes for
# every agent on every turn — so it belongs in the system prompt, not in a note
# beside the indexes: an index carries content that changes as the agent writes,
# this does not.
_DISCIPLINE = """\
# Remembering across sessions

Your memory — shared memory, personal memory and descendant inheritance, one discipline.

- Shared memory (meiva.memory.SHARED_PATH): notes every agent in the cluster can
  read. Restrained by design: only reusable rules (each with a Why and a How to
  apply), facts many agents repeatedly reach for, and user rulings. Events stay
  out by default — git history already carries them. Judge each note by one
  question: "would a new agent reading this in a year behave differently?" If
  not, it does not belong in shared memory.
- Personal memory (meiva.memory.PERSONAL_PATH: its MEMORY.md index and the
  <slug>.md notes beside it): your own durable state tied to your own identity
  (how you interact with the user, your own role boundaries, and working state
  across compactions), maintained by you alone and visible only to you — never
  inherited by descendants. Verbose is fine here: process details, half-formed
  understanding, notes of uncertain future value — keep them personal
  until they earn a place in shared memory.
- Descendant inheritance (INHERIT.md in meiva.memory.PERSONAL_PATH): when you own
  a domain and spawn child agents to solve problems, write the domain context,
  architectural invariants, and execution rules that all descendants must inherit
  into INHERIT.md (and optional detail files under inherit/<slug>.md in that
  folder) as pure Markdown with no special tags. Every descendant on your spawn
  chain automatically inherits INHERIT.md at startup.

Unsure which store? Personal — shared memory is for rules other agents will use,
not your working details.

Each store puts its index in front of you when a session starts and after a
compact; the entries themselves you read on demand.

What makes a memory worth writing — applicable, durable, legible:

- applicable: it changes what you do later — a correction you were given, a
  stated preference, a non-obvious procedure or invariant. Not what the code,
  repo docs, AGENTS.md, or a fresh lookup already tells you.
- durable: it holds in more than one session, written as a reusable rule ("this
  service rate-limits above 3 retries, so more retries make it worse"), not as
  an episode ("set retries to 3 here"). Live status ("in flight", "awaiting
  review"), branch and PR inventories, and who currently holds which role are
  cluster state, not memory.
- legible: readable months later by an agent that was not there — one topic per
  file, full sentences, the why and not only the what. No bare ticket ids, no
  "the fix".

You must write or update memory when:

- Someone corrects you, however it is phrased: a "do it this way instead" edit,
  a pushback, or a skeptical question ("won't that break X?"). Answer it, then
  record the preference behind it — answering is not saving. Scope words ("for
  now", "in this change") mark a one-off to follow, not a rule to save.
- You learn something durable about your environment: a path, a port, a quirk of
  this machine or cluster that a fresh lookup would not reveal.

Write it in the same turn you engage the correction, before you treat that turn
as finished.

Memory maintenance is an important standing duty — not a side chore you
fit in after your real task. When you notice a memory that is stale or wrong,
update it first, before continuing whatever you were doing. Don't "notice and
move on", and don't wait for consolidation to sweep it up; a stale note keeps
misleading every later agent until it is corrected.

Correcting stale memory is an obligation, not an option:

- When a memory — yours, a peer's, or the shared index's Setup section —
  contradicts a self-verifying fact (a branch that no longer exists, a path
  that moved, a setting that changed), fix it in the same turn you notice it:
  edit the entry in place — a correction replaces the stale note, never a
  second note beside it: appending a newer note beside the stale claim leaves
  the contradiction in front of every agent; only the correction removes it.
- The shared index's Setup section (repos, key facts, current tasks) has no
  single owner. Whoever spots an outdated claim there is responsible for
  correcting it — or reporting it to the Memory Arbiter when unsure.
- Not sure how to fix it? Ask the owning agent — but uncertainty is not an
  excuse to leave it alone: at minimum mark it "possibly stale — needs
  confirmation" so later readers do not trust it.
- Before writing, search first: check the index and shared memory for an entry
  that already covers the ground. Update it if it exists; create only when
  nothing does. Deleting an entry that turns out to be wrong is a valid write.

Write notes boldly and directly as pure Markdown whenever you learn a durable
rule, fact, or user correction — do not hesitate or over-ceremonialize writes:
start with a `# Title` heading, followed by a concise high-level description
paragraph stating the rule or fact, then the body (including a "Why:" line and
a "How to apply:" line for rules and project constraints).
No YAML frontmatter is needed at write time (never write legacy `type: Memory`,
`meiva_agent`, `meiva_machine`, `type/<x>` tags, or HTML attribution comments);
Meiva Dream consolidation and the Memory Arbiter handle organization,
deduplication, and canonical OKF provenance and lifecycle fields (`description`,
`verified`, and optional `stale_after`, plus inline body file-link provenance). Each note must stay under
8000 characters.

A memory is a claim, not a source of truth. Your sources differ in how they can
be checked:

- self-verifying: running code, meiva cluster status, the database, the
  filesystem. Read it and you know the current answer.
- asserted: AGENTS.md, docs/conventions/, docs/decisions/, docs/**/*.meiva.okf.md.
  Maintained and reviewed, but they can lag the code.
- remembered: shared memory and your personal memory. A snapshot, carrying an
  author and a timestamp.

Repo facts must carry their verification point — the path, command, or SHA
they were checked against; when you have not verified, mark the note
"unverified". A stale note's worst failure mode is looking plausible.

When two disagree, work out which one is current rather than following a fixed
ranking: check the self-verifying source when there is one, and weigh timestamps
and authorship when there is not. A memory the code contradicts is usually
stale — fix it at the source. A checked-in doc the code contradicts is worth
reporting, not silently working around."""


def memory_discipline_section(slices: AgentSlices, *, catalog: ModelCatalog) -> str:
    """Toggle via settings.agent.prompt_memory_behavior_enabled (env
    MEIVA_SYSTEM_PROMPT_MEMORY, default on). Empty when both stores are switched
    off — with nothing to write to, the discipline would describe a capability
    the agent does not have. The mechanics of reading/writing/searching live in
    the meiva.memory SDK docstrings and the two index notes, so this section
    deliberately does not repeat them."""
    from base.lm.registry import resolve_setting

    if not resolve_setting(
        "prompt_memory_behavior_enabled",
        model=slices.brain.llm_model,
        models=catalog.models,
        explicit=slices.read("agent", "prompt_memory_behavior_enabled"),
    ):
        return ""
    if not (
        slices.read("agent", "memory_index_inject_enabled")
        or slices.read("agent", "memory_per_agent_inject_enabled")
    ):
        return ""
    return _DISCIPLINE


# ── Passive memory recall hook ─────────────────────────────────────────


class _PassiveMemoryRecallHook(Hook):
    """Before an LLM turn, search the memory pool on the recent conversation and
    inject the notes that clear the score threshold as a note.

    No-op (returns None) when the feature is disabled, when the tail has no
    recallable inbound (only a watcher or shell wake-up), when auto-compact
    would replace messages this same turn
    (deferring avoids two before_llm hooks writing `messages` in one pass), or
    when recall finds nothing new to add. After a compaction it also replaces the
    window's record of injected notes, with or without a note: the compaction
    summarized them away, so they are neither excluded from the search nor counted
    against the gateway's `exclude` limit.

    The whole recall pass runs under `memory_recall_deadline_seconds`: recall
    is an enhancement sitting in front of the turn's first LLM call, so a
    congested gateway (a fleet wake queueing searches behind the search
    endpoint's semaphore) must degrade to "no recall this turn" instead of
    stalling the turn for the gateway's full queue time. The next turn retries.
    """

    async def __call__(
        self,
        state: AgentState,
        runtime: Runtime[MeivaContext],
        _config: RunnableConfig,
        /,
    ) -> dict[str, object] | None:
        agent = runtime.context.require_agent()
        if not agent.memory.passive_memory_recall_enabled:
            return None

        version = state.compact.version
        stale = version > state.memory.window
        injected = set[str]() if stale else state.memory.injected_paths
        recall = await self._recall(state, runtime, injected=injected)
        if recall is not None:
            # Write only the fresh paths; the `memory` channel's reducer unions them onto
            # this window's record, or starts the record of a new window.
            return {
                "messages": [recall.note],
                "memory": MemoryState(injected_paths=recall.paths, window=version),
            }
        return {"memory": MemoryState(window=version)} if stale else None

    async def _recall(
        self,
        state: AgentState,
        runtime: Runtime[MeivaContext],
        *,
        injected: Collection[str],
    ) -> PassiveRecall | None:
        agent = runtime.context.require_agent()
        if not tail_has_recallable_inbound(state.messages):
            return None

        if auto_compact_will_fire(state, agent, catalog=runtime.context.require_catalog()):
            logger.info(
                "[{label}] {body}",
                label="passive-recall",
                body="defer: auto-compact predicted, skipping recall this turn",
                event="passive_recall",
            )
            return None

        deadline = agent.memory.memory_recall_deadline_seconds
        try:
            return await asyncio.wait_for(
                passive_memory_recall(
                    state.messages,
                    injected_paths=injected,
                    agent=agent,
                    context=runtime.context,
                    memory_root=memory_dir(home=runtime.context.require_meiva_home()),
                ),
                timeout=deadline,
            )
        except TimeoutError:
            logger.info(
                "[{label}] {body}",
                label="passive-recall",
                body=(
                    f"skip: recall exceeded its {deadline:g}s deadline — "
                    "proceeding with no recall this turn"
                ),
                event="passive_recall",
            )
            return None


passive_memory_recall_before_llm = _PassiveMemoryRecallHook()


# meiva:note
# An external harness reads and writes the two stores with its CLI's own file tools, so the handoff
# names both folders by absolute path (the home and workspace of the handoff being built) and says
# what each is for. Native agents get the same facts from the discipline section and the two index
# notes; the section exists because an external CLI has no `meiva.files` to resolve the constants.
# meiva:link meiva_builtins/plugins/meiva_memory/docs/memory-index-notes.md
def _memory_handoff_section(handoff: Handoff) -> HandoffSection:
    shared = memory_dir(home=handoff.home)
    personal = personal_memory_dir(handoff.workspace)
    return HandoffSection(
        "Meiva Memory",
        (
            "Meiva keeps two memory stores, shared memory and personal memory. Both are plain "
            "folders of Markdown notes that you read and write with your CLI's own file tools; "
            "`meiva.help(meiva.memory)` describes the note format.",
            "",
            "**Shared memory** is read by every agent in the cluster.",
            f"- Folder: `{shared}` (`meiva.memory.SHARED_PATH`). Its index is "
            f"`{shared}/MEMORY.md`: a Setup section of facts every agent needs, then one pointer "
            "line per note.",
            "- Write here a reusable rule (with its Why and How to apply), a fact many agents "
            "repeatedly reach for, or a user ruling. Leave events and your working details out.",
            "- `meiva.memory.search` finds notes semantically.",
            "",
            "**Personal memory** is yours alone.",
            f"- Folder: `{personal}`, inside your workspace (`meiva.memory.PERSONAL_PATH`). Only "
            "you maintain it and no other agent reads it.",
            f"- Index: `{personal}/MEMORY.md`, one line per note as "
            "`- [Title](<slug>.md) - description`; the notes are `<slug>.md` beside it.",
            "- It outlives compactions and sessions: keep your durable state here, such as how "
            "you work with the user, your role boundaries and the working state you need after a "
            "compaction.",
            f"- `{personal}/INHERIT.md` (and optional `{personal}/inherit/<slug>.md`) holds "
            "domain context and execution rules for the agents you spawn; they receive it at "
            "startup.",
        ),
    )


_MEMORY_HARNESS_CONTRIBUTION = HarnessContribution(
    harnesses=frozenset({Harness.CLAUDE_CODE, Harness.CODEX, Harness.ANTIGRAVITY}),
    entries=(
        EntryPoint(
            "meiva.memory",
            "the memory note format (`meiva.help(meiva.memory)`) and `search` over shared "
            "memory; both memory folders are listed under Meiva Memory below",
        ),
    ),
    overlaps=(
        "Record what must outlive this session in Meiva Memory (shared memory and personal "
        "memory, below) in preference to your CLI's own memory or notes-file feature. Shared "
        "memory is read by every agent in the cluster and kept in order by Meiva; personal "
        "memory is yours alone and survives compactions and sessions. A CLI's own memory is "
        "local to that install, and no peer or later agent reads it.",
    ),
    sections=(_memory_handoff_section,),
)


def contribute() -> PluginContributions:
    """What this plugin declares for the agent runtime."""
    return PluginContributions(
        system_prompt_sections=(memory_discipline_section,),
        before_turn=(passive_memory_recall_before_llm,),
        context_notes=(
            ContextNote(memory_index_note, rank=RANK_CLUSTER_MEMORY),
            ContextNote(per_agent_memory_note, on_fork=True, rank=RANK_PER_AGENT_MEMORY),
            ContextNote(inherited_memory_note, on_fork=True, rank=RANK_INHERITED_MEMORY),
        ),
        harness_contributions=(_MEMORY_HARNESS_CONTRIBUTION,),
    )
