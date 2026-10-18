---
type: doc
title: Framework built-in sections
description: "Every section `build_system_prompt` registers before a plugin's: the Conduct group, the Capabilities group and what each section tells the agent."
tags: []
---

# Framework built-in sections

**Conduct group**:
- `_prefer_sdk_section` — "Prefer SDK"
- `_codeact_section` (in `agent/graph/prompt/_codeact.py`, registered by `system_prompt`) — "CodeAct — batch work into fewer calls": batch known operations when no intermediate review is needed. Toggle `MEIVA_SYSTEM_PROMPT_CODEACT` (default on; explicit cluster settings and per-agent overlays can disable it).
- `_keep_it_simple_section` — "Keep It Simple"
- `conversation.user_reply_section` — Always-on reply, language and milestone rules: ordinary text is self-contained, substantive communication with the user. Follow user language, default to meaningful milestones, preserve answers/questions/deliverables, and skip routine operations or wake/peer courtesy summaries. See [prompt architecture](../../../docs/prompt-architecture.md).
- `_communication_style_section` — Optional `MEIVA_AGENT_COMMUNICATION_STYLE`: `off` (default; omitted), `oriented` (live pairing), `concise` (brief milestones), `silent` (one human-requested report plus blockers/questions). Per-agent overlays may select pairing; core language and text usefulness remain active.
- `_output_conciseness_section` — Output conciseness
- `_outcome_reporting_section` — Honest reporting
- `_action_caution_section` — Confirm before irreversible actions
- `_align_before_action_section` — Resolve material outcome, cost, autonomy, and authority choices; respect existing consent rather than reapprove settled work after planning
- `_cross_machine_delegation_section` — One sentence (user-finalized wording, verbatim): when work spans machines, let an agent on the target machine do it rather than reaching across. Toggle `MEIVA_SYSTEM_PROMPT_CROSS_MACHINE_DELEGATION` (default on); semantic steer only — no API detail, so it cannot go stale.
- `delegation_check_section` — Consult capabilities and existing ownership; proactively parallelize: fork when the child needs this conversation, else a fresh agent from a written brief; a fork does not shrink the parent's window. Omit the index step and renumber when the index is empty.
- `_file_driven_work_section` — File-driven workflow
- `_long_running_operation_section` — Lifecycle and cost principles without fleet; the long-running-agent skill supplies procedures.
- `_temporal_awareness_section` — Time awareness, including the `meiva-workflow.capability-timescale` skill invoke at scheduling, estimation, and feasibility-judgment moments
- `meiva_memory.memory_discipline_section` — Cross-session durable-knowledge behavior
- `_invest_in_the_future_section` — Framework's one cross-domain future-signal rule; `MEIVA_SYSTEM_PROMPT_INVEST_FUTURE` defaults on and selects the smallest closing action for a signal that could improve later work
- `agent_workspace_pointer` — Workspace description (id-free for fork safety; the concrete path is stated by the agent-ID context note)

**Capabilities group** (lives in `capabilities.py`, registered by `system_prompt` so the render order stays the reading order):
- `capabilities_section` — The skill + MCP index. `*` selects the full catalog, but folds descendants under real entry skills with counts and inspection paths. Namespace-only folders retain leaves. Explicit lists render every selected entry; all skills stay reachable through `meiva.help(meiva.skills)`.
- Header prose names only the halves actually rendered.
- Descriptions flatten to one line, capped at 300 characters. The initial index has no total cap.

Parent: [[agent/graph/docs/system-prompt/system-prompt.meiva.okf.md|System Prompt]].
