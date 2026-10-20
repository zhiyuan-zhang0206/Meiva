---
type: doc
title: Harness onboarding
description: Install, verify and sync the external coding CLIs (claude, codex, agy, dsh) across machines, and the credential-copy verdicts behind the sync gate.
tags: [scripts, deploy, harnesses]
---

# Harness onboarding

`harness_onboarding.py` puts the four external coding CLIs on a machine, and
copies the state of a machine that is already set up to the ones that are not.
It exists because a second machine needs three different things that do not
travel the same way: the **binary** comes from its vendor, the
**configuration** can be copied, and the **login** may refuse to be copied at
all.

The credential verdicts, the measurements behind them and the operational
runbook live with the harness documentation rather than here:
`meiva_builtins/skills/platform/meiva-guide/deploy/references/harness-distribution.md`.

## Commands

```bash
.venv/bin/python scripts/host_ops/onboarding/harness_onboarding.py doctor --verify \
    --hosts macmini company-mini
.venv/bin/python scripts/host_ops/onboarding/harness_onboarding.py install agy --hosts company-mini
.venv/bin/python scripts/host_ops/onboarding/harness_onboarding.py sync dsh \
    --from macbook-air --to company-mini --dry-run
```

Hosts are `~/.ssh/config` names; `local` is the machine running the script.

## The three modules

| Module | Owns |
|---|---|
| `catalog.py` | the vendor install command, the configuration files and the login stores of each harness, plus the rotation verdict `sync` gates on |
| `hostfs.py` | the file half of a remote machine, executed there as one JSON request on stdin |
| `transport.py` | one command line on one named machine, under one pinned PATH |

`hostfs.py` runs on the target because a credential path means nothing anywhere
else; the request travels as a Python literal appended to the source, so no
secret reaches an argv (visible in `ps`) or a temporary file. `transport.py`
sends a script on stdin (`bash -s`) whenever the call carries no payload of its
own, so no command is re-parsed by a second shell.

## What the probe decides, and what this tool adds

`doctor` asks the platform probe (`base/agents/birth/harness.py`) on every
machine. That probe reads the filesystem only: it never starts a process, so it
cannot hang on a login prompt or spend quota, and it reports `unknown` rather
than guess.

`--verify` adds the two checks the probe deliberately does not make: whether the
installed file actually executes (`<binary> --version`) and whether the CLI
itself reports a login (`claude auth status`, `codex login status`). The columns
stay separate on purpose. A row whose sources disagree — the probe says
`available`, the CLI says signed out — is printed with the disagreement instead
of being resolved silently.

## Install bookkeeping

An npm install asks `npm root -g` where the global packages would land and whether this
user can write there. A distribution node on Linux answers `/usr/lib/node_modules`, which
needs root: rather than dying on `mkdir`, the command falls back to
`--prefix "$HOME/.local"`. A user-local install is then only useful where the machine's
own processes look, so the tool checks `command -v <cli>` under the agent search path
(`transport.AGENT_PATH`, no profile, no `~/.local/bin`) and links the binary into
`/usr/local/bin` when that directory is writable; when neither works it prints the exact
`sudo ln -sf` line instead of an npm stack.

Every `install` appends one line to the receiving machine's
`~/.meiva/onboarding/install-log.jsonl`: what version was asked for, what
arrived, and by which method. The vendor installers are pinned in `catalog.py`
except `agy`, whose installer publishes only a moving manifest — for that one
the log line is the only durable record.

## Sync discipline

`sync` matches the target's CLI version to the source's by re-running the
vendor installer, never by copying a binary, so signatures and self-updaters
stay intact. It refuses to downgrade a target that is newer than the source and
says so, because the newer machine is then the better source.

Configuration is copied unconditionally. A login is copied only when the store's
rotation is `static`; otherwise the copy is withheld with the reason printed, and
`--accept-credential-risk` copies it anyway. Every file overwritten on the target
is first backed up under `~/.meiva/backups/onboarding-<date>/`.
