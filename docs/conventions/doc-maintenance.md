# Doc maintenance

How documentation is structured and maintained. Read when writing or maintaining
project docs.

## Comments, docstrings and documentation

Keep implementation explanations close to the code and system explanations at
their documentation owner:

- Source comments use exact references to typed contracts in the component's
  documentation. Delete narration and duplicate prose. The supported languages,
  whole-file migration gate and closed forms are defined in
  [structured code comments](engineering/structured-code-comments.md).
- Agent-visible SDK docstrings remain help content, generated from `api` records.
  Follow [SDK docstring discipline](sdk-docstring-discipline.md) for their content.
  Internal prose belongs at its documentation owner.
- Component-local `docs/` explain responsibilities, cross-file relationships and
  usage. Cross-cutting rules belong in `docs/conventions/`; design alternatives
  and trade-offs belong in `docs/decisions/`.

Keep each explanation at one owner and link to it where useful. Remove stale or
redundant explanations when changing code. If understanding an implementation
requires extensive commentary, first consider clearer names, smaller functions
or a simpler design. Do not impose comment-count or comment-ratio budgets: judge
whether an explanation adds information that the code and its owner do not.

## Five axes, one fact per place

Every documented fact belongs to exactly one axis, and the axis is identifiable
from the path alone:

| Where | Question it answers | Tense |
|---|---|---|
| `<package>/README.md` or component `docs/*.md` | what the system **is** — package boundaries, responsibilities, design rationale | now |
| `docs/decisions/` | **why** it was chosen this way — rejected alternatives, trade-offs | past, never rewritten |
| `future/` | what we **plan** to do | future |
| `docs/conventions/` | **how** to work — shared rules, processes and coordination | now |
| `docs/postmortems/` | **why a failure escaped** — what broke, why every safety net missed it, what guardrail now prevents the class | past, never rewritten |

A fact carried on two axes will drift. When you find a duplicate, keep the copy
on the axis that owns the question and replace the other with a pointer.

### What the system *does* is not an axis

There was a fifth axis, `traces/` — one real recorded run, annotated. It is
retired ([why](../decisions/engineering/design/simplification/2026-08-19-retire-the-traces-doc-axis.md)) and
nothing trace-shaped is committed to this repo. A committed run is a fact about
a version that has already moved: it rots on every CLI rename and behavior
change, and the evidence it copied is still queryable, and current, in the
checkpoints table and the observability stack.

Do not re-create it. A question about what a run did is answered by querying
that run — `.agents/skills/inspect-a-trace/SKILL.md` is the know-how for doing
so across checkpoints, Loki, and Tempo. What generalizes out of a run belongs
on an axis: a structural fact in the package's `README.md` or `docs/`, a rule in
`docs/conventions/`, a rejected alternative in `docs/decisions/`.

## Postmortems and defensive patterns

`docs/decisions/` and `docs/postmortems/` are both past-tense and both frozen, and they are
easy to confuse. The split is the question, not the tense: a decision is a
point-in-time **choice** with alternatives rejected; a postmortem is an **escape
analysis** — the safety nets that should have caught something and did not. A
change that rejected an option is a decision even if it followed an incident; an
incident whose value is "here is why nothing caught it" is a postmortem even if
no alternative was ever on the table.

**Entry bar — all three at once:** subtle (the mechanism had to be re-derived the
hard way), systemic (it escaped through a gap in tests, tooling, or conventions,
not one person's slip), and costly to rediscover (hours, or a production
incident). A routine bug that a test caught stays in git history.

**Shape.** `docs/postmortems/NNNN-<kebab-title>.md`, numbered, executive-summary
first. Copy `docs/postmortems/_template.md`, which carries the sections: summary,
timeline, root cause (mechanism *plus* the per-safety-net escape analysis),
guardrails added, lessons. Postmortems are the one tier where war-story
chronology belongs — current-state facts stay on the other axes, cited by link.

**The pipeline.** A postmortem produces guardrails; the guardrails that
generalize condense into one 3–6 line rule in
[`defensive-patterns.md`](defensive-patterns.md), which is the page people
re-read. Neither half works alone: the rules without the stories lose their
evidence and decay into vibes, and the stories without the page never compress
into anything anyone reads twice. A postmortem whose lesson generalizes and does
not appear on that page is only half filed.

Like `docs/decisions/`, `docs/postmortems/` is skipped entirely by
`scripts/content_lint/check_doc_references.py` — naming the flag or file that existed at
incident time is the record working as intended. Nothing checks those links, so
mark what a reader cannot open; commits and PR numbers predating the 2026-08-18
public-repo cutover are not reachable from public `main` and are labelled
`(pre-cutover)`.

## Package `README.md` and component `docs/` own structure

Anything specific to a package or subsystem — its boundary, responsibilities,
non-obvious design invariants, and cross-file contracts — lives beside the code
in an optional `<package>/README.md` (for a single high-level package overview)
or standard Markdown files under `<package>/docs/` (when a package has multiple
focused design documents), never in `docs/conventions/`. Cross-domain overviews
live under top-level `docs/`.

Write standard Markdown:

- Do **not** use the retired `.meiva.okf.md` filename suffix, `type: doc` YAML
  frontmatter, fixed `## Overview` / `## Design` / `## Behavior` boilerplate
  headings, or exhaustive symbol/service rosters.
- Use standard relative Markdown links (`[label](path.md)`) so readers and
  editors can follow them directly.
- Reference validity (CLI flags and relative links across tracked docs) is
  checked by `scripts/content_lint/check_doc_references.py` (`check-doc-references`
  in pre-commit and CI).

## What does NOT go in the doc axes

Personal, strategic, and deployment-specific material does not belong in
this public tree — keep it in your own private storage (a private skills
repo, the cluster memory pool):

- Personal skills (IM, mail, social-media adapters tied to user accounts)
- Deployment instance details (machine roster, IPs, CI fleet config)
- Strategy/competitor notes
- Raw run/result histories of your own deployment

Meiva's doc axes must remain publishable as-is — no personal accounts, no internal
strategy, no deployment secrets. Before writing: "would I be fine with a
stranger reading this on GitHub?"

## Scan mapping: code change → doc to reconcile

Reconcile in the same PR as the code.

Structure changes → the documentation in `<package>/README.md` or `<package>/docs/` of the package you touched, plus
its domain overview when the domain's shape changed. Paths below are the files' real
paths:

| Change | Domain node |
|---|---|
| Agent lifecycle / crash-resurrect | `agent/docs/agent.md` |
| SDK surface (`meiva/__init__.py`, new namespaces) | `meiva/docs/meiva.md` |
| Gateway routes / SSE / auth | `gateway/docs/gateway.md` |
| CLI commands / cluster lifecycle | `cli/docs/cli.md` |
| Frontend | `ui/web/docs/web.md` |
| Base library / LM providers / config / migrations | `base/docs/base.md` |
| Background services | `services/docs/services.md` |
| GitHub Actions / CI workflows | `.github/README.md` |
| Plugins / extension points | `docs/plugins/plugins.md` |
| Skills | `docs/skills/skills.md` |
| MCP integrations | `docs/mcps/mcps.md` |
| Test suite | `tests/docs/tests.md` |
| Ops scripts | `scripts/docs/scripts.md` |
Process, rule, and observed-behaviour changes → the doc that owns them:

| Change | Doc |
|---|---|
| Shared operating principles and coordinated recovery order | `docs/conventions/`; keep component details at their owner |
| Concrete component operations | The owning domain’s `docs/`; skills link that source when guiding the capability |
| Backup schedule / retention / restore | `services/backup/` component docs; the operating skill links the coordinated procedure |
| Runtime structure (clusters, data plane, logging, CI) | Component-local OKF nodes; `runbook.md` is an operator entry and coordination guide |
| Dev environment setup | `dev-setup.md` |
| Contributor process | `docs/contributing.md` |
| Coding conventions | `python-conventions.md` |
| Agent communication style | `communicating-with-user.md` |
| Design philosophy / deliberate omissions | `philosophy.md` + `non-goals.md` |
| SDK docstrings (`meiva/*.py` public API) | `sdk-docstring-discipline.md` |
| Lint vs sweeper boundary | `lint-vs-sweeper.md` |
| Observability surfaces (event fields, span attributes, query recipes) | `.agents/skills/inspect-a-trace/` — the skill IS the reconcile target, since nothing else records what a run looks like |
| A bug class that already shipped here | `defensive-patterns.md` — plus a `docs/postmortems/` entry when it clears the entry bar |

A directional decision — one that rejected alternatives — also gets a new
`docs/decisions/<domain>/<subject>/YYYY-MM-DD-<topic>.md`. Superseding one means writing a new file
and forward-linking from the old, never editing the old.

A failure that cleared the entry bar above also gets a new
`docs/postmortems/NNNN-<kebab-title>.md`, and its generalizable lesson an entry in
`defensive-patterns.md`. Same freeze rule: a postmortem is never rewritten to
match today's code.

## Doc language

Follow the project's primary language (inferred from README / existing docs).
