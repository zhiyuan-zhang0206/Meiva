# Why a harness shows unavailable

The spawn picker asks one question — can this build start an agent, on this
machine, on this harness — and answers it with one word. A greyed row prints
`unavailable` for three different underlying states, so the word on screen is not
a diagnosis. This page is the diagnosis: what each state proves, the order to
check it in, and the two cases where a machine looks signed out and is not.

## One bit on screen, three states underneath

`GET /api/cluster/machines/<name>/harnesses` answers with one row per harness: a
`status` (`available` / `unavailable` / `unknown`), a readable `reason`, the
resolved `binary`, and `on_path`. The picker keeps only `launchable` — a launcher
for that harness exists in this build **and** the machine reports `available`
(the built-in `meiva` runtime needs neither) — and prints the same word for
everything else. The row stays on the wire so this page, and you, can tell the
three apart.

| `status` | what it proves | what it does not |
|---|---|---|
| `available` | the CLI was found, and either the CLI itself or the stored credential shows a live sign-in (`meiva`, the built-in runtime, is `available` without either) | that a session starts: the probe starts none and spends no model quota |
| `unavailable` | the binary was not found on PATH or in a conventional install directory, **or** the CLI reports no sign-in, **or** a credential that cannot renew itself has expired | that the machine has no login — see *Two false alarms* |
| `unknown` | the CLI is installed, and no login state could be read: the status command timed out or answered nothing recognizable, and the credential store is missing, unreadable, or holds only an account record with no token | **not** "signed out". `unknown` means nobody could prove it either way |

`unknown` blocks the spawn exactly like `unavailable` does. The picker is right
to; only the reason is unproven.

The launcher prints a line of its own before a session starts —
`harness: claude=2.1.290 [/usr/local/bin/claude] features=[...] decision=pty` —
and can say `unknown` there too. That one means the binary or its version could
not be read, and it says nothing about a login. Same word, different question.

Read the row before answering anyone:

```python
import meiva

for row in meiva.agents.harness_availability():
    print(row.harness, row.status, row.binary, row.on_path, "|", row.reason)
```

That call describes **the machine you are running on**. A spawn on another
machine reads that machine's row; the cross-machine view is `doctor`, in step 4.

Both readings can lag reality, differently. The picker's row is the answer for
that machine kept for 30 seconds — an install or a sign-in does not change faster
than that, and the form reopens often — while `harness_availability()` asks again
on every call, running the harness CLIs, so a call takes a few seconds when `agy`
is among the rows (its catalogue fetch dominates; measured 4.5 s for all three).
So a machine someone just fixed can still show `unavailable` in the picker for
that half-minute; one call from an agent settles it.

## The check, in the order the probe runs it

### 1. Is the binary there, and did PATH find it?

```bash
command -v claude codex agy
```

`command -v` is not the probe's answer. The probe is a different process, and a
harness session starts from an allowlisted environment that carries no PATH at
all — which is why the probe and the Claude/Codex/Antigravity launchers share one
directory list instead of trusting PATH alone. The probe's order is its own
PATH, then:

`~/.local/bin`, `~/.npm-global/bin`, `~/.bun/bin`, `~/.claude/local`,
`/usr/local/bin`, `/opt/homebrew/bin`

`on_path=false` in the row means PATH alone missed it and one of those
directories held it. That is not a fault — an agent-launched process has a
trimmed PATH — but it does mean a `command -v` in your own shell is not
evidence. An install that landed anywhere else (a distribution `/usr/bin`, a
custom prefix) is invisible to the probe until PATH happens to include it.

### 2. What does the CLI say about its own login?

The probe runs the located binary through the environment a session would be
handed, under a bounded timeout:

| Harness | Command | Bound |
|---|---|---|
| `claude_code` | `claude auth status --json` | 8 s |
| `codex` | `codex login status` | 8 s |
| `antigravity` | `agy models` — fetches the model catalogue | 12 s |

Ask the same question yourself:

```bash
unset ANTHROPIC_API_KEY; claude auth status --json   # loggedIn, authMethod
codex login status                                   # "logged in" / "not logged in"
agy models                                           # a catalogue, or a sign-in prompt
```

Unset the key first: with `ANTHROPIC_API_KEY` exported, `claude auth status`
reports `authMethod: api_key` even when a subscription login exists, so the
answer describes your shell instead of the machine. The probe builds its own
environment, so its answer does not inherit that key either.

A timeout, a binary that will not run, or output that names no state leaves the
probe unproven, and it falls back to the stored credential.

### 3. What does the stored credential prove?

| Harness | Store | Counts as signed in |
|---|---|---|
| `claude_code` | `~/.claude/.credentials.json` | a token whose refresh deadline is in the future. A past access-token expiry is only the next refresh, not a logout |
| `codex` | `~/.codex/auth.json` | an API key, or a ChatGPT token set. The file records no expiry, so `last_refresh` is reported as age |
| `antigravity` | `~/.gemini/antigravity-cli/antigravity-oauth-token` | an access token; its expiry is reported as age, because the CLI renews from the refresh token |

An account name is not evidence: `~/.claude.json` keeps `oauthAccount` after the
login behind it died, so a run that carries only that record reads `unknown`.
Neither store read starts a session or spends quota, so a row is cheap — and
that is also its limit: it does not prove the login works today.

### 4. Across machines

From the Meiva source checkout:

```bash
.venv/bin/python scripts/host_ops/onboarding/harness_onboarding.py doctor --verify \
    --hosts macmini company-mini company-air macbook-air ubuntu
```

`doctor` prints the probe's own row per machine. `--verify` adds, as separate
columns, whether the installed file runs (`<binary> --version`) and what the CLI
itself says about its login. A row whose sources disagree — probe `available`,
CLI signed out — is printed with the disagreement rather than resolved silently;
treat the probe column as unverified on that row. For Antigravity the login
column stays empty: `agy` has no non-interactive status subcommand.

## Two false alarms

**1. macOS: a login kept in the Keychain reads as signed out over SSH.** On
macOS, Claude Code keeps its token in the login Keychain. A process on an SSH or
agent chain runs in a non-GUI context where that Keychain cannot be unlocked for
it, so the CLI answers "not logged in" for a login that works in the graphical
session. Measured on a macOS host three times, including with the Keychain
unlocked. Such an answer is not evidence about the machine.

```bash
launchctl managername    # Aqua = GUI session; Background = SSH / agent chain
```

Confirm the login from a terminal in the desktop session, or judge it in the same
context that will run the session. The same trap is why a macOS row that turned
`unavailable` is worth re-reading before anyone signs in again.

**2. `~/.claude/.credentials.json` is often absent on macOS, and its absence
means nothing.** Once Claude Code moved the token into the Keychain, a correctly
signed-in macOS machine can have no such file at all. Judging by the file reports
"not signed in" for a machine that is signed in. That is why the probe asks the
CLI first and reads a file only as a fallback.

## Say what you know

A row — any of the three — is a statement about an installed binary and a
credential store. The only proof that an agent will run is a real spawn on that
harness: spawn one, give it work that needs a tool call, read the turn, then stop
it. That path has been walked for `claude_code`; the [Antigravity](antigravity.md)
and [DeepSeek Harness](deepseek_harness.md) pages say what is still unproven
there.

Never report `unknown` to a human as "not installed" or "not logged in": it means
this machine could not read the login state. When that answer is not good enough,
sign the CLI in once in the graphical session and read the row again.

The `reason` is written to be read out. When it already names the fix (`Run
claude once and sign in`, `Run agy once in a terminal to confirm it is signed
in`), that sentence is the answer to give; the steps above are for when it does
not settle the question.

## Where the pieces live

- the probe, the harness rules and the three states:
  `base/agents/birth/harness.py`
- the install directories and the PATH a session gets:
  `base/host/env/harness_binaries.py`
- why the picker shows one word:
  `ui/web/src/components/agents/harness-picker.tsx`
- installing a CLI and copying state between machines:
  `scripts/host_ops/docs/harness-onboarding.meiva.okf.md`
