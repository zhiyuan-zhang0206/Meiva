# Claude SDK session bridge demo

One command runs a real Claude Agent SDK session through the four things the
bridge exists for: a turn, an approval, a cancel, and a resume.

```bash
uv sync --extra claude-sdk      # once: the SDK is an optional extra
.venv/bin/python demos/claude-sdk-bridge/bridge_demo.py
```

## What it does

| Stage | What happens | What proves it |
|---|---|---|
| 1. talk + read | one message, one turn; the model reads `hello.txt` | the answer contains the file's third word |
| 2. approval | `Write` is denied by the demo's policy | no `approval.txt` on disk, and the model says so |
| 3. cancel | a `Bash` `ping` starts, then `interrupt()` | the cancel report's `settled` is true and `remaining` is empty |
| 4. resume | a new process resumes the first session | the model recalls the number from stage 1 |

The demo prints wall-clock time per stage, the interrupt latency, the child
reaping result, and the token/cost totals the bridge recorded.

## Requirements

- A Claude subscription login on the machine running it (`claude auth status`
  reports `authMethod: claude.ai`). The demo never sets or reads an API key; an
  `ANTHROPIC_API_KEY` exported in the shell is blanked for the child.
- Network access to the provider, and enough quota for a few hundred output
  tokens. A full run costs a fraction of a cent at list price.

## Notes

- The demo writes only inside a fresh temporary workspace.
- `Stage 3` interrupts as soon as the tool's child process is observable; that is
  the same signal the bridge's cancel assertion uses (`base/lm/claude_sdk/process.py`).
- If the model declines to run the `ping`, stage 3 times out after 60s and the
  demo exits non-zero rather than reporting a false pass.
