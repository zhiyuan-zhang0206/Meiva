# skill-creator skill — Create/modify/review skill

## What it is
An **authoring and evaluation method** for Meiva skills
(`$MEIVA_HOME/skills/skill-creator/`), informed by Anthropic and OpenAI and adapted
to Meiva's tools. `SKILL.md` carries the concise workflow;
`../meiva-guide/references/evaluation.md` is the single shared evaluation
contract, bundled in Guide for complete external publication.
`evals/evals.json` and `evals/metrics.md` carry the creator's own
authoring cases and grading definitions. Run artifacts stay outside the package.

`references/audit.md` owns catalog-audit criteria and primary guidance.
`evals/catalog-audit.json` and `evals/catalog-audit-metrics.md` carry portable
metadata-routing and operational-navigation probes. These are offline evaluator
inputs; the metrics distinguish packaging checks, proxies, and live execution.

## Judgments carried
- **Three-level loading model**: metadata (name+description, always in context) / SKILL.md body (loaded when triggered) / bundled resources (scripts/references/assets, on demand). When writing a skill, distribute content across these three levels — description should let the agent determine "when to reach for it."
- **When to create a skill**: when an instruction has no existing config field to carry it, and will be used repeatedly, creating a small skill is cleaner and more reusable than piling into config: no carrying config field exists, so create a small skill.
- **skill vs plugin**: skill is a pure markdown instruction package, no runtime state; to modify agent behavior / inject hooks, use plugin.
- **Evaluation is a deliverable**: cases, metric definitions, acceptance criteria,
  and a baseline precede execution. Results retain per-case output/trace evidence
  and execution gaps; incomplete evaluation remains a draft. Quality and
  efficiency are reported separately.
- **Discovery vs execution**: descriptions are precise rather than broadly
  pushy. Implicit activation is evaluated separately from an explicitly loaded
  or preloaded skill's task behavior.
- **Harness boundary**: the case-file convention is not a new runtime API.
  The existing self-evolution harness accepts replay-safe dataset records and
  produces proxy scores; task correctness needs its own checks.

## Key dependencies
- [Skills index](../../README.md) — full skills catalog
- [Skill System](../../../../meiva/skills/README.md) — created things land in this mechanism
- [Plugin system](../../../../docs/plugins/plugins.md) — boundary between skill and plugin
- [Self-evolution](../meiva-self-evolution/docs/meiva-self-evolution.md) — existing trace replay, isolation limits, and proxy scoring
