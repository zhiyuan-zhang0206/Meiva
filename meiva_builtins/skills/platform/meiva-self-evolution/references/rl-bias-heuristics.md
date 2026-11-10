# RL bias heuristics for persistent multi-agent systems

Read this reference when repeated user corrections or operational friction
suggest a mismatch between episode-shaped behavior and persistent Meiva work.
These are diagnostic hypotheses, not established claims about a particular
model's training. Confirm each against recorded runs, available capabilities,
and the user's actual authority before choosing a mitigation.

## Common RL Biases in Multi-Agent Persistent Systems

### 1. Solo-Execution Bias

The working hypothesis is that single-agent RL episodes favor doing everything
within one agent. Look for two distinct symptoms:

- **Ownership Trespass:** bypassing a specialist whose Memory and Workspace
  hold domain context, duplicating work or overriding an existing decision.
- **Refusal to Delegate:** a coordinator writes code or inspects files itself
  despite independent work and an available specialist or sub-agent.

Check responsibility and capability before attributing the failure. Direct
execution remains appropriate for short, tightly coupled work. When delegation
helps, route to the existing owner or give a child an explicit brief, evidence
requirements, and a return path.

### 2. Stateless Sandbox Bias

The working hypothesis is that stateless training sandboxes lack persistent,
logged-in browser GUIs. An agent may prematurely request API tokens, passwords,
or manual clicks without checking an authorized logged-in `meiva.mcps.chrome`
session for Google, GitHub, AWS, Vercel, Cloudflare, or another service.

Verify the host, browser access, account, and allowed action first. Prefer a
sufficient connected tool when available; otherwise use the authorized browser
session. Login availability does not grant authority to publish, change account
settings, or expose credentials. Request user input only for a concrete gap.

### 3. Synchronous Episode Bias

The working hypothesis is that blocking, single-threaded episodes favor one
foreground task at a time. In persistent work, recency can displace unfinished
obligations: an agent opens a PR, handles newer messages, then forgets its CI,
review feedback, or merge handoff.

Look for a missing completion event, durable pending record, or responsible
owner. Retain the PR/task identity, next condition, and reporting destination;
use an existing watcher or managed background wait to resume on that condition.
A new message does not close an earlier obligation.

### 4. Terminal-Reward Horizon Discounting (gamma=0 after task completion)

Use `gamma=0` as a diagnostic metaphor: reward at tests passing or PR creation
can give post-task hygiene near-zero effective weight. Look for stale git
worktrees, temporary repositories, and disk caches accumulating even with an
"Invest in the future" instruction.

Check whether an apparent leftover still supports active work before calling it
waste. Record completion and cleanup separately, with ownership and safe
eligibility conditions. Repeated misses suggest a mechanism beyond inline
reminders.

## 5-Level Escalation Hierarchy for Mitigating RL Biases - Never Pollute Core

Evaluate strictly top-down. Select the first sufficient level, documenting why
any earlier level cannot cover the observed need. The levels select a remedy;
they do not authorize runtime changes, schedules, deletion, or external actions.

### Level 1 - Adapt to the RL Bias via Existing Runtime Primitives

Let the agent act synchronously as trained while existing runtime mechanisms
absorb concurrency. Consider frequent forks where a forked child blocks until
completion, non-destructive interrupt-to-background, or auto-backgrounding a
blocking command. Use only mechanisms supported by the current harness; an
interrupt must preserve the underlying work and its completion delivery.

A command such as `gh pr checks --watch && gh pr merge` illustrates a wait
followed by an action. Use it only with separately authorized merge authority
and the repository's required-check policy; otherwise wait and notify the
maintainer. Background work needs a durable owner, bounded lifetime, and a
completion or failure wake-up. Reuse existing watchers before adding another.

### Level 2 - Memory (Declarative Facts, Rulings & Ownership Routing)

Store declarative facts, user rulings, and specialist ownership routing in
Memory. Examples include who owns a domain and which accounts are logged in on
host Chrome. Promote reusable private facts to shared Vault Memory at
`~/.meiva/vault/memory/` after checking existing entries and verifying the fact.
Include its verification point and update stale facts in place. Store account
availability and routing, never credentials. Keep transient task status in task
records rather than durable shared Memory.

### Level 3 - Skill (Reusable Procedures & Playbooks - between Memory and Plugin)

When the recurring pattern is a reusable multi-step procedure, SOP, or workflow,
extract or refine a Skill (`SKILL.md`). Static Memory notes fragment a procedure;
a Plugin adds unnecessary machinery when instructions are sufficient. A Skill
may include helper scripts or reference checklists. Define its trigger, steps,
permissions, completion evidence, and failure handling, then evaluate affected
cases through the skill-authoring workflow.

### Level 4 - Conditional Scheduler (Low-Frequency Threshold-Gated Schedule)

For post-reward hygiene that repeatedly escapes inline rules, propose a general,
low-frequency Schedule, for example every 12 hours, with a lightweight threshold
pre-check. Below threshold it silently does nothing; above threshold it acts only
on eligible resources within explicit cleanup authority.

Examples include stale git worktrees, disk cleanup, and forgotten green PRs.
Specify the threshold, active-work exclusions, owner, failure path, and evidence
of completion. Check for existing schedules first. A green PR can trigger a
handoff; merging it still requires merge authority. Protect live sessions,
uncommitted work, recoverable state, and needed caches; a threshold alone does
not make deletion safe. Load the scheduling and cleanup playbooks before any
authorized implementation.

### Level 5 - Isolated Plugin (Never Pollute Core)

Only when Levels 1-4 cannot cover the need and a new tool or service surface is
required, build an isolated Plugin. Explain each earlier level's insufficiency,
the new surface, its permissions, lifecycle, and verification. Keep case-specific
primitives, such as a dedicated worktree primitive, out of Core.

## Evidence required in a proposal

Record the real run ids and observed symptom, the suspected bias and alternative
explanations, the selected level and reasons earlier levels are insufficient,
the concrete edit or authorized mechanism, and the completion check. Distinguish
static checks from behavioral evidence. Reuse the evaluation loop for justified,
replay-safe cases; report unexecuted comparisons explicitly.
