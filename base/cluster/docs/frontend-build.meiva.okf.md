---
type: doc
title: Frontend build reuse
description: When the frontend's production build is rebuilt and when it is reused, and what makes the reuse safe.
tags: [base, cluster, frontend]
---

# Frontend build reuse

The frontend service's launch command starts with
`base.cluster.frontend_build ensure` and only then `exec pnpm run start`. `ensure`
runs the Next.js production build when the build's inputs changed since the last
successful build, and reuses the existing `.next` output when they did not.

## Why

`pnpm run build` was ~12 s on a warm Next.js cache and ~35 s cold (TypeScript
typechecking plus Turbopack compilation), and `meiva start` previously paid it on
every backend-only commit when the build key bound the monorepo's `HEAD`. A
start whose frontend inputs did not change should reuse `.next` in ~0.1 s, and a
start whose frontend inputs did change should hit warm `.next/cache/turbopack`
and `.next/cache/.tsbuildinfo` without triggering an implicit `pnpm install`.

## The key

`build_key` hashes:

- `frontend_source_digest(frontend_dir)` — the relative paths, file modes and
  SHA-256 contents (or symlink targets) of every tracked or untracked,
  non-ignored file under `ui/web` (`git -C ui/web ls-files --cached --others
  --exclude-standard -z`), deliberately **without** hashing the monorepo's
  `HEAD` commit SHA. Commits outside `ui/web` keep the exact same digest and
  reuse `.next`. Gitignored outputs (`.next`, `node_modules`, `*.tsbuildinfo`,
  `next-env.d.ts`) are outside it by construction, so a build does not
  invalidate its own key.
- `node --version`.
- the `NEXT_PUBLIC_*` values baked into the bundle (`gateway_port`,
  `browser_origins`, the ordered list of same-origin browser entries).

The key is recorded at `ui/web/.next/meiva-build-key` **after** a successful
build. A start whose key matches the recorded one skips the build; every other
start builds and then records.

## Why the reuse is safe

- **A failed or interrupted build cannot look current.** The key is written only
  after `pnpm run build` exits zero. An interrupted rebuild still carries the
  previous key, whose inputs differ from the current ones by construction.
- **Deleting the build deletes the claim.** The key file lives inside `.next`.
- **An unprovable or mid-flight key builds.** A checkout git cannot read, a file
  whose `lstat` changes while being read, a file list that changes mid-scan
  (`frontend_source_digest` raises), or a missing `node`, yields `None` — and
  `None` always builds.
- **A node_modules that drifted from the lockfile or workspace settings is
  caught before the build decision**: `ensure_frontend_deps` runs first and
  reinstalls on a `pnpm-lock.yaml` / `pnpm-workspace.yaml` change or drift, and
  both files are part of `frontend_source_digest`. Pinning
  `enableGlobalVirtualStore: false` and `verifyDepsBeforeRun: false` in
  `ui/web/pnpm-workspace.yaml` keeps `CI=true pnpm install --frozen-lockfile`
  and non-CI `pnpm run build` / `pnpm run start` on identical workspace state
  and prevents `pnpm`'s pre-script hook from spawning an unfrozen install that
  would dirty `node_modules` timestamps against `.next/cache/turbopack`.

## Forcing a rebuild

Delete `ui/web/.next` (or just `ui/web/.next/meiva-build-key`). The next start
rebuilds.

## Boundaries

- Nothing here skips a *correctness* gate: the type check, the bundle build and
  the `exec` on the serve stage all still run whenever a build runs.
- The command shape stays a single compound whose final stage execs, so
  `base.sessions.env_forwarding.exec_into` keeps accepting it. That shape is not
  cosmetic: the 2026-08-27 outage was a frontend restart whose command lost its
  `exec`, so the session validator rejected it and a dead frontend could never
  self-heal.
- The serve stage passes the port explicitly (`-p <port>`): Next.js would
  otherwise default to 3000, and a restart would silently move the app
  off-cluster.
