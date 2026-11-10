# meiva-guide.external-agents skill — External harness agents and client skills

## What it is

`meiva_builtins/skills/platform/meiva-guide/external-agents/` (`$MEIVA_HOME/skills/meiva-guide/external-agents/`) is the `meiva-guide` sub-skill for running and diagnosing first-class external harness agents (`claude_code`, `codex`, `antigravity`) and for understanding how `meiva start` publishes `meiva-guide` into external client skill directories.

## Judgments carried

- **First-class harness agents, not terminal workers**: Claude Code, Codex, and Antigravity run as numbered Meiva agents via `meiva.agents.spawn(..., config_overlay={"harness": ...})` and `meiva.agents.restart(..., config_overlay={"harness": ...})`. `agent-host` owns their turn lifecycle, mid-turn batch steering, checkpointing, and resume, and attaches the full `meiva.*` SDK surface into their environment via `meiva harness -c "..."`. Agents do not launch coding CLIs inside `meiva.shell.sessions` PTY terminals or coordinate through task/work files.
- **External provenance on CLI sends**: External coding sessions operating from a shell inspect and message live agents through the host's `meiva` CLI (`meiva agents ls`, `meiva agents timeline <id>`, `meiva agents send <id> <text> --source external_agent:<subject>[:<instance>]`), keeping external-agent turns distinct from human operator turns.
- **Availability is three states behind one word**: The spawn picker greys a row with a single `unavailable`, and which probe state (`available` / `unavailable` / `unknown`) sits behind it decides the advice. The diagnosis order, the per-harness login checks, and the two macOS false alarms (a Keychain login read over SSH, an absent `~/.claude/.credentials.json`) live in `references/harness-availability.md`.

## Published copies

`meiva start` publishes this sub-skill's parent guide into the global skill directory of each external agent surface it finds (Codex, Claude Code, Gemini CLI, Qwen Code, Cursor Agent, Antigravity App/IDE and CLI, DeepSeek Harness), so a session can read them without the Meiva checkout. `references/supported-clients.md` is the matrix of those directories with its evidence, the presence rule, duplicate visibility, and the tools deliberately left out.

## Key dependencies

- `meiva_builtins/skills/README.md` — full skills catalog
- `meiva_builtins/skills/platform/meiva-guide/agents/SKILL.md` — agent lifecycle verbs, config overlays, and harness switching / handoff contract
- `base/agents/birth/harness.py` — harness availability probe and status model
- `cli/commands/extensions/external_skills.py` — host-global external client skill convergence
