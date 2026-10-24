---
type: doc
title: System Prompt — System Prompt
description: "The system prompt built once per context window. Constructed by `build_system_prompt(extensions, slices, agent_id=...)` in order: base guidance → SDK overview → behavior conventions → capability index → plugin sections."
tags: []
---

# System Prompt — System Prompt

## What it is

The Meiva harness system prompt carried in every native LLM call, built **once per context window** — `init_context` renders it when it lays the standing head down (an agent's first wake, and the turn after each compaction), never per turn. Constructed by `build_system_prompt()` in registration order: base guidance → SDK overview → behavior conventions → capability index.

External harnesses use their own provider CLI system prompts. `init_context` does
not build the native `execute_code` prompt, its plugin sections, capability index
or standing notes for them; instead, it lays down `SystemMessage(handoff.render())`
at `messages[0]` (`harness_cursor = 0`) so the handoff document sent to the
external harness is recorded at `0.0` in the conversation timeline.

## Core Mechanism

### build_system_prompt (`agent/graph/prompt/system_prompt.py:build_system_prompt`)
- The base is the `_BASE_SYSTEM_PROMPT` constant in `agent/graph/prompt/_base_prompt.py` (the `{_MEIVA_OVERVIEW}` placeholder injects the SDK overview; `{_SEQUENTIAL_TOOL_CALLS}` takes the sequential tool-call sentence when `MEIVA_SYSTEM_PROMPT_SEQUENTIAL_TOOL_CALLS` is on, default off)—**it never reads `AGENTS.md` at runtime**
- Appends SDK documentation (the output of `meiva.help(meiva)`)
- Runs `FRAMEWORK_SECTIONS` in fixed order, then the registry's plugin sections
- Injects the skill and MCP server index — **once**. `# Capabilities` is the sole index; the expanded SDK reference's `*` skips `meiva.skills` / `meiva.mcps` (`CAPABILITY_SURFACES`) so it renders call contracts only, never a second capability listing

### What the prompt reads, and where

The prompt decides from values. `build_system_prompt` reads the running process once, in `agent/graph/prompt/live.py:read_live_sdk(prompt)`: the skill catalog (`meiva.skills.names()`), the configured MCP servers, the plugin-declared expansions and the disable entries (the configured `MEIVA_SDK_DISABLE` list followed by the removals applied at run time). Those four facts travel as one `LiveSdk` value, and the decisions over them are functions of values: `sdk_scope.py` (disable matching, `expand_plan`), `skill_selection.py` (`SkillView`, which skills a list selects, the drift diff) and `capabilities.py` (the index text, recall candidates, the drift note). The three modules are `strict` in `meizel.toml`, so a read of the process, the clock or the environment cannot creep back in. Names a configured list cannot resolve are returned, not logged, by the decision; the reader that built the view reports them. Tests build the catalog on disk (real skill folders scanned by the production scanner), hand the resulting `SkillView` in and assert the text; none of them patches `settings`, the install registry or the SDK installation.

### Plugin Declaration (`PluginContributions.system_prompt_sections`)

The builder passes its catalog to sections from `MeivaContext.extensions` after the framework groups.
The public signature, declaration order and external-plugin migration contract live in
[[docs/plugins/declared-contributions.meiva.okf.md]].

### Framework built-in sections

`FRAMEWORK_SECTIONS` registers the Conduct group first and the Capabilities group
last; the Capabilities group lives in `capabilities.py` and is registered by
`system_prompt` so the render order stays the reading order. Every framework
section takes the same frozen `SectionInputs` (`agent/graph/prompt/live.py:SectionInputs`:
the agent slices, the model catalog and the `LiveSdk` read) and ignores the parts
it does not need, so a registered section can never be called with the wrong
shape. Plugin sections keep their declared `(slices, catalog=...)` contract. Each section's
wording and toggle: [[agent/graph/docs/system-prompt/framework-sections.meiva.okf.md|framework built-in sections]].

### Capability index freshness

The rendered index is a snapshot over an uncached filesystem scan, so a skill
installed mid-window is reachable by name before the listing names it;
`state.capabilities.indexed` and a framework `before_llm` drift hook close that
gap. [[agent/graph/docs/system-prompt/capability-index-freshness.meiva.okf.md|Capability index freshness]].

## Entry Points

- `agent/graph/prompt/system_prompt.py:build_system_prompt(extensions, slices)` — Build the complete prompt
- `base/packages/plugins/extensions.py:PluginContributions` — what a plugin's `contribute()` declares
- `agent/graph/prompt/capabilities.py:capabilities_section(view, servers)` / `agent/graph/prompt/skill_selection.py:resolve_prompt_skills(wanted, view)` — the `# Capabilities` index and the name→skill resolver it shares with the preloaded-skills note
- `agent/graph/prompt/skill_selection.py:indexed_skills(view)` / `index_drift(known, view)` — what the index covers right now, and the diff against a snapshot of it; `agent/graph/prompt/live.py:skill_view(prompt)` reads the view
- `agent/graph/hooks/capabilities.py:newly_installed_skills` — the `before_llm` hook that names skills installed since the index was built
- `agent/graph/_init_context.py:init_context_node()` — caller of `build_system_prompt()`, and where the snapshot is recorded

## Notes

- System prompt + history messages + current turn output = LLM call
- The prompt text is directly aimed at the agent (audience = agent, not developer), must be all English, and must not expose internal implementation names
