# Skills catalog

Each repo skill is a self-contained directory under `meiva_builtins/skills/<group>/<name>/`
(`SKILL.md` + optional `scripts/` for runnable code, `references/` for
material the agent reads and adapts, `assets/` for templates and static
files). The source-tree groups are `practice/` (engineering, research, review),
`coordination/` (running and supervising agents, schedules), `platform/`
(operating, extending and documenting Meiva itself) and `integrations/`
(mail, audio, web); a group is a folder without a `SKILL.md`,
and converge lands each skill flat at `skills/<name>/`, so the group never
reaches a skill's identity. This index lists the skills that carry their own
OKF node, grouped by what each does — those headings cut across the source-tree
groups and are informational only.

Descriptions identify the capability and the situations that need it; keyword
mentions alone do not select an operational workflow. Entrypoints retain shared
decisions and constraints, while substantial mode procedures, examples, and
source context load from references only when relevant. Catalog audits follow
the criteria and evaluation cases owned by `skill-creator/references/audit.md`.

## Communication & user interaction
Launch web pages to display content or collect structured replies, read SMS
verification codes on macOS, run live immersive sessions with the human, and a
full Gmail client.

| Skill | Purpose | Detail |
|-------|---------|--------|
| gmail | Full Gmail client (read/search/send/reply/forward/draft + newsletter, pure stdlib IMAP/SMTP) | [gmail skill — Full Gmail Client](integrations/gmail/README.md) |
| meiva-immersive-mode | Run a tight-loop live session across a shared perceptual surface while heavy work runs in the background | [meiva-immersive-mode skill — tight-loop live sessions with the human present](coordination/meiva-immersive-mode/README.md) |

## Ops, scheduling & lifecycle
Operating and extending Meiva, scheduling timed tasks, background watchers, and
behavioral discipline for long-running agents.

| Skill | Purpose | Detail |
|------|------|------|
| meiva-guide | Shared guide for humans and internal/external agents: deploy, operate, extend, schedule, and manage agents; root routes to sub-skills | [Meiva Guide — Understand, deploy, operate, and extend Meiva](platform/meiva-guide/README.md) |
| meiva-being-a-long-running-agent | Operating as a long-running process: manage lifecycle, wait for external events, persist before compaction | [meiva-being-a-long-running-agent skill — Long-Running Process Discipline](coordination/meiva-being-a-long-running-agent/README.md) |

## Orchestration & workflow
Breaking large tasks into multi-agent / long tasks and driving them.

| Skill | Purpose | Detail |
|------|------|------|
| meiva-workflow | Select working strategy across alignment, capability and timescale, goals, planning, coordination, and evaluation | [meiva-workflow skill — Choose how to work](practice/meiva-workflow/README.md) |
| meiva-dynamic-workflow | Orchestrate parallel workers: explore→fork→join→reduce | [meiva-dynamic-workflow skill — script orchestration](coordination/meiva-dynamic-workflow/README.md) |
| meiva-goal | Sustain a terminal goal with evidence checks and optional peer supervision | [meiva-goal skill — Sustain pursuit of a terminal goal](coordination/meiva-goal/README.md) |

## Self-improvement
Meiva improving itself — mining regressions, reviewing/creating skills, and
tech debt.

| Skill | Purpose | Detail |
|------|------|------|
| meiva-self-evolution | Weekly collect real runs into trace dataset, mine skill/plugin regressions and produce fix reports | [meiva-self-evolution skill — Mining regressions from real runs](platform/meiva-self-evolution/docs/meiva-self-evolution.md) |
| skill-creator | Create / improve / review skill | [skill-creator skill — Create/modify/review skill](platform/skill-creator/README.md) |

## Web & media
Fetching content from the web and media — driving AI web apps, per-source
adapters, transcription.

| Skill | Purpose | Detail |
|------|------|------|
| web-sources | Fetch content from any source; per-platform adapter subs: generic / rss / youtube | [web-sources skill — Fetch content from any source](integrations/web-sources/README.md) |

## Not indexed here
The remaining top-level built-ins (such as `meiva-corp`,
`meiva-serious-engineering` and `meiva-serious-research`)
have no OKF node; their `SKILL.md` is the reference. Capability/timescale
calibration belongs to `meiva-workflow`; deployment, incident operations,
modification layers, installation, plugin development, schedules, and external
agent execution and page composition belong to `meiva-guide`. Temporary waiting
contracts belong to `meiva.watcher`; waiting and recovery methods belong to
`meiva-being-a-long-running-agent`. Model speed and feedback settings belong to `meiva-guide`'s models skill.

## Key dependencies
- [Skill System](../../meiva/skills/README.md) — skill mechanism and core-vs-instance origin axis
