---
type: doc
title: System Prompt — System Prompt
description: "The system prompt built once per context window. Constructed by `build_system_prompt(extensions, slices, agent_id=...)` in order: base guidance → SDK overview → behavior conventions → capability index → plugin sections."
tags: []
---

# System Prompt — System Prompt

## What it is

The system prompt carried in every LLM call, built **once per context window** — `init_context` renders it when it lays the standing head down (an agent's first wake, and the turn after each compaction), never per turn. Constructed by `build_system_prompt()` in registration order: base guidance → SDK overview → behavior conventions → capability index.

## Core Mechanism

### build_system_prompt (`agent/graph/prompt/system_prompt.py:build_system_prompt`)
- The base is the `_BASE_SYSTEM_PROMPT` constant in `agent/graph/prompt/_base_prompt.py` (the `{_MEIVA_OVERVIEW}` placeholder injects the SDK overview; `{_SEQUENTIAL_TOOL_CALLS}` takes the sequential tool-call sentence when `MEIVA_SYSTEM_PROMPT_SEQUENTIAL_TOOL_CALLS` is on, default off)—**it never reads `AGENTS.md` at runtime**
- Appends SDK documentation (the output of `meiva.help(meiva)`)
- Runs `FRAMEWORK_SECTIONS` in fixed order, then the registry's plugin sections
- Injects the skill and MCP server index — **once**. `# Capabilities` is the sole index; the expanded SDK reference's `*` skips `meiva.skills` / `meiva.mcps` (`_CAPABILITY_SURFACES`) so it renders call contracts only, never a second capability listing

### Plugin Declaration (`PluginContributions.system_prompt_sections`)

The builder passes its catalog to sections from `MeivaContext.extensions` after the framework groups.
The public signature, declaration order and external-plugin migration contract live in
[[docs/plugins/declared-contributions.meiva.okf.md]].

### Framework built-in sections

`FRAMEWORK_SECTIONS` registers the Conduct group first and the Capabilities group
last; the Capabilities group lives in `capabilities.py` and is registered by
`system_prompt` so the render order stays the reading order. Each section's
wording and toggle: [[agent/graph/docs/system-prompt/framework-sections.meiva.okf.md|framework built-in sections]].

### Capability index freshness

The rendered index is a snapshot over an uncached filesystem scan, so a skill
installed mid-window is reachable by name before the listing names it;
`state.capabilities.indexed` and a framework `before_llm` drift hook close that
gap. [[agent/graph/docs/system-prompt/capability-index-freshness.meiva.okf.md|Capability index freshness]].

## Entry Points

- `agent/graph/prompt/system_prompt.py:build_system_prompt(extensions, slices)` — Build the complete prompt
- `base/packages/plugins/extensions.py:PluginContributions` — what a plugin's `contribute()` declares
- `agent/graph/prompt/capabilities.py:capabilities_section(slices)` / `resolve_prompt_skills()` — the `# Capabilities` index and the name→skill resolver it shares with the preloaded-skills note
- `agent/graph/prompt/capabilities.py:indexed_skills(prompt)` / `index_drift(known, prompt)` — what the index covers right now, and the diff against a snapshot of it
- `agent/graph/hooks/capabilities.py:newly_installed_skills` — the `before_llm` hook that names skills installed since the index was built
- `agent/graph/_init_context.py:init_context_node()` — caller of `build_system_prompt()`, and where the snapshot is recorded

## Notes

- System prompt + history messages + current turn output = LLM call
- The prompt text is directly aimed at the agent (audience = agent, not developer), must be all English, and must not expose internal implementation names
