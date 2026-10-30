---
type: doc
title: Memory checkout operations
description: Memory checkout refresh, role-aware initialization and gateway bundle bootstrap.
tags: [base, memory, git]
---

# Memory checkout operations

`memory_repo.py` owns Git operations and bundle installation on an explicit
checkout. The builtin memory plugin setup owns provisioning decisions. Steward and arbiter agents own authoring, push/PR and consolidation;
this module does not schedule those responsibilities. The plugin's scaffold derives its paths from the home in its `ScaffoldFacts`
through the foundation path layer, avoiding a dependency on the SDK's memory facade. Repository operations do not discover
a home, global settings or a default working directory.

## Refresh the reader's checkout

The gateway passes the same captured checkout that its memory reads use. It
captures keep-local and track-branch when the gateway application is built;
the Git operation does not rediscover machine roles, paths or global settings.
The legacy name `pull_main` does not restrict the branch to literal `main`.
The indexer independently observes checkout changes; it is not a caller of
`pull_main`. Refresh completion proves a checkout update, not embedding completion.

## Initialization and ownership

Runner authoring uses machine-<name>, including combined gateway/runner units. The consolidated gateway checkout uses main and a separate directory on combined units. Only a gateway-only authoring checkout may automatically leave a stale machine branch; runner work must not be switched implicitly. Keep-local initialization removes remotes.

Initialization's `subprocess.run` calls use argv lists with the Git executable
fixed; configured remote and checkout values remain individual operands, never
shell source. The existing S603 annotations preserve that reviewed call shape.
The named memory exceptions describe observable states and are consumed by CLI
and plugin setup; N818 annotations retain those existing names.

A fresh split runner without a remote bootstraps the gateway bundle or fails loudly; it never silently starts with an empty shared pool. Bundle ancestry and advertised HEAD are checked before installation, and the temporary bundle origin is removed. An existing initialized checkout is not re-bootstrapped.

The fresh checkout is prepared in a temporary directory and moved into place
after removing pre-init leftovers; that removal and replacement are not one
atomic transaction.
Initialization does not select this path for an authored Git checkout. Gateway-capable first boot can initialize locally
because its gateway need not be serving yet. The operation receives its `GatewayAccess`
explicitly: the builtin scaffold takes it from the `ScaffoldFacts` that `meiva memory init`
builds from its command runtime.

`init(root, branch, remote)` performs a selected remote clone, branch selection
or publication and missing gitignore commit. `reconcile_initialized` checks a
selected existing checkout; only a caller with the gateway main-switch
capability may leave its old branch. The scaffold chooses local initialization,
remote cloning or gateway bootstrap only after checking the target derived from its facts.
`is_initialized(root=...)` retains its `.git` directory test, without extending
it to Git worktree marker files.

GitHub preflight also keeps configuration selection at the CLI root: existing
role/local-only/opt-out gates run first, `github_auth_blocker(host)` checks the
program and authentication, and only success reads the existing global remote.
`memory_remote(raw)` validates it, `memory_repo_slug(remote)` parses it and
`github_permission_blocker(host, slug)` checks write permission. Neither Host
probe reads settings; no combined compatibility alias remains.

## Bounded I/O and status

`run_bounded` owns descendant cleanup; `subprocess.run(timeout=...)` alone
would only terminate its direct child. Bounding Git also bounds a refresh
worker that otherwise holds executor shutdown open. The existing bound is per
command, not one deadline for an entire fetch/merge sequence.

Pool downloads enforce both declared and streamed size limits and report transport failures as MemoryPoolBootstrapFailed, preventing an unbounded response from filling memory before installation.

Repository status receives an explicit root, track branch and timezone. It
renders FETCH_HEAD's modification time in that supplied zone. There is currently
no production status caller or configuration root supplying these values; the
explicit API replaces the old lazy global-clock read without adding a getter. The existing RepoStatus value and MemoryRepoUninitialized error remain the status surface; inspecting status does not initialize a repository.

`RepoStatus.is_clean` covers the working tree and index. The local commit count
is the entire HEAD ancestry; ahead/behind compare to the supplied track
branch, despite the historical field names ending in `_main`. Without that
remote reference they remain zero. `last_fetch` renders FETCH_HEAD's mtime in
the supplied time zone, or `never`; an uninitialized repository raises
`MemoryRepoUninitialized` rather than reporting an empty repository.

Parent: [[base/deploy/docs/deploy.meiva.okf.md]].
