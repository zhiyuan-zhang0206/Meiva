# Capabilities skill paths and subtree unfolding

`meiva.help(meiva.skills)` already rendered each skill's description and `SKILL.md`
path, while `agent/graph/prompt/capabilities.py` maintained a separate index
formatter that omitted the file path and instructed agents to call
`meiva.help(meiva.skills.<path>)` first. On external harnesses (`claude_code`,
`codex`, `antigravity`), resolving that path cost a 5-15s Python SDK cold start,
and wildcard selection (`skills_to_inject_into_system_prompt = ["*"]`) folded all
descendants unconditionally — hiding `meiva-workflow`'s five general playbooks
(`align`, `capability-timescale`, `define-goal`, `evaluate`, `plan`) from the
always-on index.

`base.packages.skills.names:skill_md_path` now provides the single `SKILL.md`
path projection shared by `meiva.skills.skill_proxies` (`meiva.help`) and
`agent.graph.prompt.capabilities` (`# Capabilities`, `new_skills` drift notes,
and `skill_recall` notes), and top-level `meiva.help(meiva.skills)` reports
`Sub-skills: <N>` on root skills. Publishing `meiva-workflow` or `meiva-goal`
into user-global CLI directories (`~/.claude/skills`, `~/.codex/skills`) was
rejected so Meiva-specific coordination skills never leak into standalone
non-Meiva CLI sessions; injecting `SKILL.md` paths through `# Capabilities`
keeps exposure scoped to Meiva sessions while letting both native and external
harnesses read skill files directly.

`skills_to_inject_into_system_prompt` now follows the same composition rule as
`MEIVA_SDK_EXPAND`: `*` selects the full catalog and folds unmentioned
descendants under their nearest visible ancestor skill, while explicit sub-skill
paths (`meiva-workflow.define-goal`) or `<namespace>.*` patterns alongside `*`
unfold those entries in place (mirrored in `ops/cluster/operations.py` for
narrowed lists). The shipped default is `["*", "meiva-workflow.*"]`, surfacing
the five cross-domain workflow playbooks while keeping domain sub-skill trees
(`meiva-guide`, `meiva-serious-engineering`, `meiva-serious-research`) folded.
`meiva-workflow/SKILL.md` also replaces the blanket prohibition on mentioning
goal contracts with plain-language guidance to propose verifiable completion
criteria, deliverable anchors, and autonomy boundaries on multi-step deliveries
while leaving atomic commands and open-ended discussions direct.

Targeted verification covers `base/packages/skills/tests/test_names.py`,
`meiva/tests/skills/test_skills.py`, `agent/graph/tests/prompt/`,
`agent/graph/hooks/tests/test_capabilities_drift.py`,
`agent/graph/hooks/tests/test_skill_recall_hook.py`,
`agent/graph/hooks/tests/test_skill_recall_question_ids.py`,
`agent/graph/tests/test_init_context.py`,
`agent/graph/tests/test_init_context_harness.py`, and `base/tests/config/`.
Recall scoring calibration and peer-gate filtering remain owned separately by
`agent/graph/recall/skill_recall.py`.
