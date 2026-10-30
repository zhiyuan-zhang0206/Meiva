---
type: doc
title: "`$MEIVA_HOME` Layout"
description: '`base/paths/__init__.py` resolves every per-unit path from `$MEIVA_HOME`. One home = one unit; co-located units keep separate state because their homes differ. `meiva_home()` reads the environment and creates the home; a pure helper `H(home)` derives a path from the home it is handed and creates nothing, while the keyword form `H(*, home)` also creates an explicit home owner-only.'
tags:
- base
- library
- filesystem
---

# `$MEIVA_HOME` Layout

## What it is

`base/paths/__init__.py` is the single-point resolver for every per-unit path. The root
comes from `base.paths.home.resolve_meiva_home` (env `MEIVA_HOME`, default
`~/.meiva`), read on every call — never captured at import; it is the ambient door of the
pure `home_from_env(env, user_home=...)`, which a function that is handed the environment
calls instead. Only
`meiva_home()` reads the environment; it creates the ambient home (mode 0700), as the keyword
helpers below create an explicit one. The composition root of a process calls it once and
passes the result down.

A helper takes one of two forms. The **pure form** `H(home)` (`traces_dir`, `skills_dir`,
`mcps_dir`, `logs_path`, `install_registry_path`, `running_sha_path`, `launch_failures_path`, the run
tree — `run_dir`, `exec_run_dir`, `root_run_dir`, `root_manifests_path`, the chrome, computer,
permissions-helper, insights and MCP-daemon sockets, `insights_pidfile`
and the `otel_collector_*` paths) derives a path from the home it is handed: it reads no
environment and creates nothing, so a test hands in `tmp_path` and the process that binds a
socket, takes a lock or writes a file creates the directory.

The ten helpers that make a directory or sit on the home itself (`plugins_dir`, `logs_dir`,
`permissions_helper_app_dir`, `memory_dir`, `gateway_memory_dir`, `workspaces_root`,
`workspace_dir`, `uploads_dir`, `harness_sessions_dir`, `harness_registrations_dir`) take a
required **keyword form** `H(*, home)`: the absolute home is created owner-only (0700, as `meiva_home()` creates the
ambient one) and the helper derives from it, reading no environment; a relative home is refused,
and a call without `home=` is a `TypeError`. `workspace_dir`, `plugins_dir`, `logs_dir` and
`uploads_dir` also create their own directory (`uploads_dir` is the staging root the gateway
receives uploads into; `uploads_path` is its pure form). `workspace_path(home, agent_id)` is the pure form of the workspace
derivation, for a reader of another agent's workspace.

`base/paths/expand.py` owns what a leading `~` in a path a person typed means: `expand_home(path,
*, user_home)` turns `~` and `~/...` into paths under a home the caller read, keeps an absolute or
relative path as written, and refuses `~user` (another account's home is not something an invocation
reads). It is `strict` and reads no home of its own.

All three process classes — gateway, agent, exec subprocess — inherit the same
`MEIVA_HOME` variable; each entry resolves it once and hands the home down. **The home is the unit's
identity**: a co-located gateway unit at `~/.meiva_gateway` and a runner unit at
`~/.meiva` share no state, because every path below derives from a different root.

## Layout

```
$MEIVA_HOME/
├── .env                        # the unit's config source of truth
├── .env.lock                   # serializes .env rewrites across processes (never .env itself)
├── source/                     # prod only — the git checkout the sessions run from
├── pg/  redis/                 # this cluster's own data-plane instances
├── run/                        # pidfiles, unix sockets, sessions/ (native-supervisor records)
├── logs/                       # <daemon>.log, agent-{N}.out.log / .stderr.log, rollout-<ts>.log
├── traces/                     # spans.jsonl + rotated spans-<ISO>.jsonl (collector mirror) + .ship-watermark.json
├── otel-collector/             # otelcol-contrib binary + config.yaml + queue/ (sidecar, task #1266)
├── backups/db/                 # daily pg_dump --format=custom, UTC-stamped, newest 7 kept
├── backups/env/                # .env snapshot taken before each config write
├── memory/                     # the memory pool git repo
├── workspaces/<agent_id>/      # per-agent working dir (delivered uploads land here)
├── uploads/<agent_id>/         # gateway staging of received upload bytes (never the agent's copy)
├── chrome-profile/             # the shared headed Chrome's dedicated profile
├── plugins_config.json         # per-machine plugin enable state
├── installed.json              # install registry (skills / plugins / MCP packages)
├── installed.json.lock         # serializes registry rewrites across processes (never the registry)
├── mcp.json  mcp_enabled.json  # machine MCP server defs + per-host enable overlay
├── skills/<name>/SKILL.md      # the single skill load dir (gated by the registry)
├── mcps/<name>/                # installed MCP packages, each with its own .venv
├── plugins/<name>/plugin.py    # externally installed plugins
└── deploy-state.*.lock         # home lifecycle mutexes (+ .holder.json diagnostics)
```

There is no separate host state directory: the host runs one cluster, so what a
host shares lives in its home — the vendored Postgres runtime (`runtime/`), the
initdb template (`pg-template-17/`), coding-session owner records
(`coding-session-owners/`) and the PTY allocation gate (`pty-allocation-freeze.json`
+ its stable `pty-allocation.lock`). Pointing `MEIVA_HOME` at a temporary directory
therefore isolates all of them. The home lists no clusters: each home describes
only itself.

## Notes

- **`.env` / `installed.json` lock discipline** (sibling file locks at every door, leaves stay leaves, atomic save vs lost update): [[base/paths/docs/lock-discipline.meiva.okf.md]].
- `workspaces_path()` owns the `workspaces` path segment (`workspaces_root()` and `workspace_path()` derive from it); `workspace_dir`,
  `meiva_code`'s fork cwd re-homing and the
  agent-runner's to_user watcher all derive from it in both directions (build a
  workspace path, or classify one as belonging to some agent's workspace). Do
  not re-derive the root as `workspace_dir(...).parent` or by composing
  `meiva_home() / "workspaces"`.
- The home is `MEIVA_HOME` when set, else `~/.meiva`
  (`base/paths/home.py:resolve_meiva_home`), never from cwd and never from a
  flag: no pointer file, no checkout claim, no in-process override. A home that
  carries its own `<home>/source` checkout is operated only by that checkout's code
  (`base.paths.home.home_checkout_error(repo, home=home)`; `prod_service_checkout_error` applies it to
  service launches). Cluster identity **is** this path — there is no cluster name;
  see [[base.meiva.okf.md|the base overview]].
- `run/` exists so ephemeral runtime artifacts (pidfiles, sockets, session
  records) do not litter the home's top level.
- Daemon pidfiles live only under `$MEIVA_HOME/run/`.
- Operational procedures that act on these paths (backup/restore, log reading,
  recovery) are in `.agents/skills/meiva-guide/operations/`.
