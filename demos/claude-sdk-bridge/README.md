# Claude SDK session bridge demo

One command runs a real Claude Agent SDK session through what the bridge is for:
handoff injection, the persisted agent-id ↔ session-id pair, a cancelled tool, and
a resume from the registry.

```bash
.venv/bin/python demos/claude-sdk-bridge/bridge_demo.py
```

## What it does

| Stage | What happens | What proves it |
|---|---|---|
| handoff | a six-section handoff is injected as the first message | the model answers its instruction (READY), and the demo prints the document's byte size |
| 1. talk + read | one message, one turn; the model reads `hello.txt` | the answer contains the file's third word |
| 2. mapping | the registry record is written | `agent 6984 -> session <id>` exists on disk |
| 3. cancel | a `Bash` `ping` starts, then `interrupt()` | `settled` is true and `remaining` is empty |
| 4. resume | a new process resumes from the registry, no handoff re-injection | the model recalls the number from stage 1 |

The demo prints wall-clock time per stage, the interrupt latency, the child
reaping result, the handoff size in bytes, and the token/cost totals.

## Requirements

- A Claude subscription login on the machine running it (`claude auth status`
  reports `authMethod: claude.ai`). The demo never sets or reads an API key; an
  `ANTHROPIC_API_KEY` exported in the shell is blanked for the child.
- Network access to the provider, and enough quota for a few hundred output
  tokens. A full run costs a fraction of a cent at list price.

## Notes

- Everything the demo writes lives in a fresh temporary workspace, including the
  session registry (`<workspace>/sessions/6984.json`).
- Stage 3 interrupts as soon as the tool's child process is observable; that is
  the same signal the bridge's cancel assertion uses (`base/lm/claude_sdk/process.py`).
- If the model declines to run the `ping`, stage 3 fails after 60s and the demo
  exits non-zero rather than reporting a false pass.
