---
type: doc
title: Start host dependencies
description: Every external executable and OS service `meiva start` needs, who installs it, and what start does without it.
tags:
- cli
- lifecycle
- readiness
- dependencies
---

# Start host dependencies

`cli/commands/data_plane/host_dependencies.py` runs one preflight as the first
cold-start converge step. It is a table (`HOST_TOOLS`), and `render_table()`
prints that table; this document carries its output, and a test in
`cli/commands/lifecycle/tests/test_start_host_dependencies.py` fails when the two
drift apart. Three kinds of row exist:

- **A provisionable group** (`data-plane`, `node`, `host-utils`): a missing tool
  runs its `scripts/provision/*` script once for the group, and the probe runs
  again afterwards. A tool still missing refuses the start.
- **A host-owned tool** (`systemctl`, `sudo`, `launchctl`, the Apple toolchain
  (`swiftc`, `codesign`, `security`, `openssl`), `/bin/sh`): this repository cannot install it, so the refusal names it
  and prints the decision or command that fixes the host.
- **An optional tool** (`git`, `gh`, `brew`, `lsof`, `ps`): start warns or skips
  the step that uses it; nothing blocks.

| Tool | When `meiva start` needs it | Who installs it | Missing → start |
|---|---|---|---|
| `initdb` | gateway with a local data plane: initializes the cluster's PostgreSQL 17 | `scripts/provision/database.sh` (apt/Homebrew) | install → re-probe; still missing → refuse |
| `pg_ctl` | gateway with a local data plane: starts and stops that instance | `scripts/provision/database.sh` (apt/Homebrew) | install → re-probe; still missing → refuse |
| `postgres` | gateway with a local data plane: the server process itself | `scripts/provision/database.sh` (apt/Homebrew) | install → re-probe; still missing → refuse |
| `pg_isready` | gateway with a local data plane: the readiness probe (a host client tool; the vendored tree omits it) | `scripts/provision/database.sh` (apt/Homebrew) | install → re-probe; still missing → refuse |
| `redis-server` | gateway with a local data plane: the cluster's own Redis process | `scripts/provision/database.sh` (apt/Homebrew) | install → re-probe; still missing → refuse |
| `redis-cli` | gateway with a local data plane: authenticated admin and ACL commands | `scripts/provision/database.sh` (apt/Homebrew) | install → re-probe; still missing → refuse |
| `pgbouncer` | gateway with a local data plane and pooling enabled: the connection pooler | `scripts/provision/database.sh` (apt/Homebrew) | install → re-probe; still missing → refuse |
| `node` | frontend or browser service selected: builds and runs the Next.js app | `scripts/provision/node.sh` (apt/Homebrew) | install → re-probe; still missing → refuse |
| `npm` | frontend or browser service selected: installs and builds the frontend | `scripts/provision/node.sh` (apt/Homebrew) | install → re-probe; still missing → refuse |
| `npx` | frontend or browser service selected: runs the frontend tooling and the meiva-browser daemon | `scripts/provision/node.sh` (apt/Homebrew) | install → re-probe; still missing → refuse |
| `crontab` | Linux, OS jobs enabled, default home: registers the daily backup, log rotation, packages refresh, health probe and boot autostart | `scripts/provision/host_utils.sh` (apt; macOS ships both) | install → re-probe; still missing → refuse |
| `systemctl` | Linux, OS jobs enabled, default home: installs and enables the boot autostart unit | the host; this repository cannot install it | refuse before converge |
| `sudo` | Linux, OS jobs enabled, default home, not root: writes the boot unit as root | the host; this repository cannot install it | refuse before converge |
| `launchctl` | macOS, OS jobs enabled, default home: loads the launchd jobs | the host; this repository cannot install it | refuse before converge |
| `curl` | frontend service selected: the frontend readiness probe | `scripts/provision/host_utils.sh` (apt; macOS ships both) | install → re-probe; still missing → refuse |
| `swiftc` | macOS with the permissions helper: compiles the helper app | the host; this repository cannot install it | refuse before converge |
| `codesign` | macOS with the permissions helper: signs the helper app | the host; this repository cannot install it | refuse before converge |
| `security` | macOS with the permissions helper: the code-signing identity in the keychain | the host; this repository cannot install it | refuse before converge |
| `openssl` | macOS with the permissions helper: key generation | the host; this repository cannot install it | refuse before converge |
| `sh` | always: every supervised service and OS job line launches through /bin/sh | the host; this repository cannot install it | refuse before converge |
| `git` | never required: records the running commit and warns about a dirty checkout | the host; this repository cannot install it | nothing: warn or skip, the start continues |
| `gh` | never required: the PR-flow sampler job, production home with a Trunk token | the host; this repository cannot install it | nothing: warn or skip, the start continues |
| `brew` | never required: the macOS Homebrew pin drift check | the host; this repository cannot install it | nothing: warn or skip, the start continues |
| `lsof` | never required: fallback listener discovery when psutil cannot name a port holder | the host; this repository cannot install it | nothing: warn or skip, the start continues |
| `ps` | never required: parent-chain inspection of launchd jobs (macOS always ships it) | the host; this repository cannot install it | nothing: warn or skip, the start continues |

### What start does not need

The same audit covered every tool that merely looks like a dependency, because
the caller that appears to need it uses a Python path instead:

- `tar`, `xz`, `gzip`, `unzip` — archives come through Python `tarfile` /
  `lzma` (`base/cluster/dataplane/runtime_binaries.py`).
- `curl` and `wget` as downloaders — vendor and bootstrap downloads use
  `urllib` (`base/host/env/bootstrap.py`). `curl` is required only as the
  frontend readiness probe.
- `jq` — JSON is parsed in Python.
- `rsync`, `scp` — cross-machine transfer goes through the gateway upload path
  or a Drive folder; no shell copy runs at start.
- `docker` — only the verification recipes use it.
- `psql`, `pg_dump`, `pg_restore`, `pg_config` — schema work and backups go
  through psycopg or the vendored client tools of the database provisioner;
  `pg_dump`/`pg_restore` are executed by `meiva backup`, not by start.

`uv` and the checkout's `.venv` interpreter are settled before start by
`uv sync` and `meiva init`; a start that cannot import Meiva never reaches this
preflight.

Parent: [[cli/commands/lifecycle/docs/start-readiness.meiva.okf.md|start readiness]].
