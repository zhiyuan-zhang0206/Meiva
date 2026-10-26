# Google Antigravity (`agy`)

Launch a persistent Antigravity session in a workspace with `spawn_antigravity.py`:

```bash
.venv/bin/python scripts/spawn_antigravity.py <workspace-dir> \
  --why 'why this launch is needed' --prompt '<initial message>'
```

Without `--prompt`, `spawn_antigravity.py` creates `tasks.md` and `work.md` (unless already present), opens an `meiva.shell.sessions` PTY running `agy chat -a <workspace>`, and sends the file-driven collaboration contract. With `--prompt`, it launches `agy chat -a <workspace>` and sends your inline message verbatim (no task/work files are created).

On success it prints one `key=value` per line:

```text
session_id=<id>
antigravity_session=<conversation-uuid>
tasks_file=<absolute path>   # only in Mode A (without --prompt)
work_file=<absolute path>    # only in Mode A (without --prompt)
```

- `antigravity_session` is Antigravity's own conversation UUID (`ANTIGRAVITY_CONVERSATION_ID`).
- Pass `--resume <antigravity_session>` to reopen that conversation with `agy chat -r <uuid> -a <workspace>`.
- For a takeover (Mode B), pass `--prompt` instructing the spawned Antigravity session to read `impersonator-guide` and run `meiva impersonate request --agent <agent_id> --provider antigravity --ttl 1800 --batch-window 0 ...` (`--thread-id` defaults to `ANTIGRAVITY_CONVERSATION_ID` when set in the Antigravity environment).

## Availability, and what it does not prove

A greyed row in the spawn picker prints one word for three different states;
which state is behind it, and the order to check them in, is
[why a harness shows unavailable](harness-availability.md). Two points are
specific to `agy`:

- **The login check is a network call.** The probe asks `agy models`, which
  fetches the model catalogue (measured 2-5 s, bounded at 12 s). A slow or
  offline machine reads `unknown`, and `unknown` blocks the spawn instead of
  guessing either way.
- **A running session is not a signed-in machine.** `agy` has no `login`, `auth`
  or `whoami` subcommand, so the only proof that a *new* process can
  authenticate is a fresh run:

  ```bash
  agy -p 'reply with exactly: OK' --print-timeout 90s
  ```

  Measured on 2026-10-20 across five machines: a live `agy` session kept working
  on a Linux host while every new process there answered `authentication
  required`, because the credentials never landed where a new process reads
  them. Judge a machine by a new process, not by a session someone left running.

The token file the probe reads is
`~/.gemini/antigravity-cli/antigravity-oauth-token`. The IDE's
`~/.gemini/jetski-standalone-oauth-token` is a different owner: a machine holding
only that file still answers `authentication required`.

Pending real-machine acceptance: #5425 (Antigravity) / #5334 (dsh). Nothing above
describes a completed spawn, turn, stop and resume on `agy`; fill that in when
they complete.
