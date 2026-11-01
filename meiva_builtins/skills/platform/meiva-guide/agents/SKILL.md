---
name: agents
description: "Manages Meiva agents and configuration overlays. Use when spawning, inspecting, restarting, stopping, or configuring agents."
---

# Agent-Level Concepts

This sub-skill covers the concepts that live at the agent layer: agents
themselves, commands, and schedules.

## Agents (running AI process)

An **agent** is a running AI process — you are one. The lifecycle verbs:

- `spawn` — start a brand-new agent.
- `fork_from` — copy another agent's conversation state into a new one.
- `terminate` — end it (default: finish current turn first; `force=True` requests
  interruption when a clean stop cannot progress; `enqueued` confirms
  acceptance, not exit or completion of owned work).
- `restart` — finish current turn, then restart as a fresh process under the
  same `agent_id` (mandatory `message` explaining the restart reason; optional
  `config_overlay` to change model, effort, or switch harness).
- `resurrect` — wake a `terminated` agent with its full conversation state
  intact (must include a `prompt` telling it why it was woken).
- `transfer` (CLI only: `meiva agents transfer <id> --to <machine> --message "..."`
  or `--no-message`) — move an `idling` or `terminated` native agent and its
  workspace directory to another machine. Running agents are refused (`409`);
  to relocate yourself, ask a peer or operator to transfer you after calling
  `meiva.self.terminate()`.

State machine: `idling (unclaimed) → running ⇄ idling → terminated`.

Agent-to-agent communication: `meiva.agents.send_message(agent_id, content)` —
pure INSERT, no status check, no wait, no delivery receipt. If the target is
`terminated`, it is auto-resurrected to handle the message. Use
`get_neighbors` to see which agents are most closely tied to you (ties form on
spawn/fork/resurrect/send_message and decay over time), and `get_ancestors`
to see the spawn chain above an agent (who spawned whom, nearest first —
the responsibility-attribution read).

Each agent can have a **label** (`meiva.self.set_label`) — a
human-readable role/name that persists once set; it is also stated in the
agent's own agent-ID context note at each window establishment.

SDK reference: `meiva.agents` (`spawn` / `terminate` / `restart` / `resurrect` /
`send_message` / `list_agents` / `list_machines` / `get_neighbors` /
`get_ancestors` / `commands` / `get_last_message`).

## Switching Harnesses & Handoff Process

An agent's harness (the runtime loop it runs on: `meiva`, `claude_code`, `codex`,
`antigravity`, `kimi_code`) can be switched bidirectionally (`meiva` <-> external,
or external <-> external) via `restart` with a config overlay:

- **SDK**: `meiva.agents.restart(agent_id, message="Switching to Codex", config_overlay={"harness": "codex"})`
- **CLI**: `meiva agents restart <agent_id> -m "Switching to Codex" --overlay harness=codex`
- **Gateway REST API**: `POST /api/agents/{agent_id}/restart` with body `{"message": "...", "config_overlay": {"harness": "..."}}`

### Preconditions, Invariants & Handoff Process
1. **Agent State**: The agent must be in `idling` or `terminated` status. A running
   agent will be rejected with `409 Conflict`.
2. **Mandatory Message**: `message` is mandatory across all restart calls (minimum
   1 non-whitespace character).
3. **Model Compatibility**: If changing harness, ensure the target harness supports
   the agent's model, or supply a compatible `llm_model` in the overlay.
4. **Handoff Document**: Before stopping or requesting a harness switch, have the
   outgoing agent write a comprehensive handoff file (`README.md` or `HANDOFF.md`)
   in its workspace root. This document should cover:
   - Workspace directories and current layout
   - Git branches and active worktree paths
   - Open tasks, active investigations, and pending decisions
   - Key architectural invariants and constraints
   - Optionally creating a git bundle or archive if switching machines
   The restart caller should then pass the path to this handoff document and a concise
   summary of current progress in the mandatory `message` argument of the restart call.

### Transition Sequence
When switching harnesses on restart:
1. The runner calls `/prepare-harness-switch` to persist a lifecycle switch note in the timeline.
2. The agent is dropped from active runner cache, and any prior harness CLI session files on disk (`$MEIVA_HOME/harness/sessions/{agent_id}.json`) are removed to ensure a clean start.
3. If `idling`, the agent is cleanly terminated.
4. `agents_meta.harness` is updated in the database, and the agent is resurrected on its new harness with a wake prompt referencing the restart message.

## Command (user-level command)

A **command** is a `/name <natural language instruction>` — issued from the
frontend Composer or via `send_message` between peers. The receiving agent's host expands
it into the prompt the model actually sees; the model never sees the `/`-trigger
process, only the expanded text. Commands are addressable prompt functions —
agents can use `meiva.agents.send_message(peer, "/name ...")` to send a named
intent with a description.

Two sources:**built-in commands** (`commands/*.md` + `~/.meiva/commands/*.md` for
user overrides) and **plugin commands** (plugins carrying a `commands/` directory,
exposed as `/plugin.name`). Every active skill also gets a free eponymous command
(calling it loads the skill via `meiva.help(meiva.skills.<name>)`).

Create a command by placing a `<name>.md` file with optional frontmatter
(`description` + `instruction-hint`) and a fixed prompt template as the body. One
command accepts one argument — the free text after `/`.

Commands are **user-level** operations ("what should the agent do this
conversation"), distinct from the `meiva` CLI (ops-level, runs in shell, operates
the deployment itself). Peers discover each other's commands via the read-only
`meiva.agents.commands()` (name + description + instruction-hint, no body).

## Schedules (time-based / condition-triggered persistent tasks)

The gateway hosts **schedules**: scripts written by agents, run persistently in
a session, auto-restarted on crash (time-triggered, event/threshold-triggered, or
both). Managed via REST API (`/api/schedules`) or the frontend
`/control/schedules` page.

To turn a natural-language scheduling need into a schedule, load
`meiva.skills.meiva_guide.schedules` and follow it (it clarifies trigger/skip/error
handling, writes a resumable script, then `POST /api/schedules` to create).


## Config adjustment (modifying system configuration)

Meiva's config surface has three layers, each with its own adjustment path. Know
which layer you're touching before picking the tool.

### The three config layers

| Layer | What it controls | Where it lives |
|---|---|---|
| **Cluster / host config** | Model keys, DB URLs, service ports, timezone, feature flags — the `.env` fields. | `$MEIVA_HOME/.env` (gateway's for cluster fields; each host's for host fields) |
| **Agent per-agent config** | Per-agent overrides: model, skills, reasoning effort — the fields a spawn/restart overlay carries. | Passed at spawn as `config_overlay` fields, snapshotted into the agent row |
| **User settings** | Frontend display preferences (timeline density, theme, etc.) — per-user, synced across frontends. | `user_settings` DB table (TanStack Query + `useUserSettings`) |

### Adjustment paths by layer

#### Cluster / host config

| Path | Command / URL | Notes |
|---|---|---|
| **CLI** | `meiva config get [KEY] [--machine M]` | Read all or one field |
| | `meiva config set KEY=VALUE [...] [--machine M]` | Write; restart hint printed |
| | `meiva config unset KEY [...] [--machine M]` | Revert to default |
| **REST API** | `GET/PUT /api/config[?machine=M]` | Same surface the CLI uses; reducer semantics (absent key = left untouched, null = unset) |
| **Frontend** | `/control/config` | Machine selector + capability sections; bool toggles, text edits, sensitive-field masking |
| **Direct .env edit** | Edit `$MEIVA_HOME/.env` then restart | Last resort; prefer `meiva config set` which validates + routes correctly |

`--machine` targets a remote agent-runner's host fields; omit for the
cluster/gateway view.

#### Agent per-agent config

Passed at spawn time — not edited on a running agent:

```python
from uuid import uuid4

# Explicit fields — model_id is an id from the live roster, see the models sub-skill
meiva.agents.spawn(prompt="...", config_overlay={"llm_model": model_id}, idempotency_key=str(uuid4()))

# Several fields at once — every key must be a per_agent field
meiva.agents.spawn(prompt="...",
                 config_overlay={"llm_model": model_id, "reasoning_effort": "high"},
                 idempotency_key=str(uuid4()))
```

Per-agent config is snapshotted into the agent row at spawn; a later
cluster-default change does not affect agents already spawned.

To change an agent's config after spawn: restart it with a new `config_overlay`
(`meiva.agents.restart(agent_id, message=...)` in SDK, or
`meiva agents restart <id> --message "..."` in CLI —
the restart path accepts a config_overlay).

#### User settings

User settings are frontend-only (display preferences, not cluster config).
Managed via the settings panel in the web UI; synced through TanStack Query
(`useUserSettings` hook, `user_settings` DB table). Not accessible from CLI or
SDK.
