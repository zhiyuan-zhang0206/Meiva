---
name: external-agents
description: "Spawns, switches, and diagnoses external harness agents (Claude Code, Codex, Antigravity) and client skill directories. Use when running an agent on an external harness or checking why a harness shows unavailable."
---

# External Harness Agents

Claude Code (`claude_code`), OpenAI Codex (`codex`), and Google Antigravity
(`antigravity`) run as first-class Meiva agents alongside the built-in `meiva`
runtime. Each external harness agent has its own `agent_id`, workspace
(`$MEIVA_HOME/workspaces/<agent_id>`), timeline, inbox, and full `meiva.*` SDK
surface (invoked from its shell via `meiva harness -c "..."`).

`agent-host` owns a harness agent's turn lifecycle (`idling` / `running` /
`terminated`), mid-turn steering, smooth drain, checkpointing, and resume
exactly like a native loop's. Do not launch `claude`, `codex`, or `agy` inside
`meiva.shell.sessions` PTY terminals or coordinate through task/work files and
watcher scripts — spawn or switch a numbered Meiva agent on the target harness
instead.

## Spawning an External Harness Agent

Use `meiva-workflow` to decide whether delegation serves the task, define the
mission and verification criteria, and name the required skills in the spawn
prompt.

Check live harness availability and supported models on the host before
spawning:

```python
import meiva

for row in meiva.agents.harness_availability():
    print(row.harness, row.status, row.default_model, row.models, "|", row.reason)
```

Spawn a fresh agent on `codex`, `claude_code`, or `antigravity` via
`config_overlay`:

```python
child_id = meiva.agents.spawn(
    prompt="Mission, required skill(s), and milestone report-back contract...",
    label="codex-auth-fix",
    config_overlay={
        "harness": "codex",          # "codex" | "claude_code" | "antigravity" | "meiva"
        "llm_model": "gpt-5.4",      # optional; must be supported by the chosen harness
        "reasoning_effort": "xhigh", # optional
    },
)
```

From the CLI:

```bash
meiva agents spawn "<prompt>" --label codex-auth-fix --overlay harness=codex
```

## Steering, Supervising, and Switching Harnesses

- **Message and steer normally:** Call `meiva.agents.send_message(agent_id, content)`
  from an agent, or `meiva agents send <id> "<text>" --source ...` from a shell.
  While a harness turn is running, non-control inbound messages (`user`,
  `agent`, `external_agent`, `system_note`) are batch-steered mid-turn into the
  active harness session. If the agent is `idling` or `terminated`, the message
  starts or resurrects its next turn.
- **Inspect progress on the timeline:** Read `meiva.agents.get_last_message(agent_id)`
  or `meiva agents timeline <id> [--limit N]`.
- **Switch an existing agent's harness:** An `idling` or `terminated` agent can
  switch harnesses bidirectionally (`meiva` <-> external or external <-> external)
  via `meiva.agents.restart(agent_id, message="...", config_overlay={"harness": "claude_code"})`
  (or `meiva agents restart <id> -m "..." --overlay harness=claude_code`). Follow
  the preconditions and handoff file contract in
  [agents](../agents/SKILL.md#switching-harnesses--handoff-process).

## External Sessions Messaging Meiva Agents

When an external coding agent session running outside Meiva (or from a shell)
inspects or messages a numbered Meiva agent on the host, use the host's `meiva`
CLI (`~/.local/bin/meiva`) with explicit external provenance:

```bash
meiva agents ls
meiva agents timeline <agent_id> --limit 20
meiva agents send <agent_id> "<message>" --source external_agent:<subject>[:<instance>]
```

where `<subject>` is `claude_code`, `codex`, or `antigravity`. Never pass
`--source user` when acting as an external agent, and never craft raw HTTP
requests against gateway endpoints.

Such a session is not a Meiva agent. It may read Meiva's shared Memory but
writes nothing there without the user's approval; see [memory](../memory/SKILL.md).
The numbered harness agents above are Meiva agents and follow the Meiva
instructions they receive.

## Availability Diagnosis and Published Client Skills

- The spawn picker prints one word — `unavailable` — for three different probe
  states (`available`, `unavailable`, `unknown`). When a harness is greyed out
  or `meiva.agents.harness_availability()` reports a problem, follow
  [why a harness shows unavailable](references/harness-availability.md) for the
  probe order, per-CLI login checks, and macOS Keychain / credential-file false
  alarms.
- `meiva start` converges this guide into the global skill directory of each
  supported external client present on the host so external sessions can read
  it without the Meiva checkout. See
  [client skill directories](references/supported-clients.md) for the client
  matrix and presence rules.
