---
type: doc
title: Frontend build reuse
description: When the frontend's production build is rebuilt and when it is reused, and what makes the reuse safe.
tags: [base, cluster, frontend]
---

# Frontend build reuse

The frontend service's launch command starts with
`base.cluster.frontend_build ensure` and only then `exec npm run start`. `ensure`
runs the Next.js production build when the build's inputs changed since the last
successful build, and reuses the existing `.next` output when they did not.

## Why

`npm run build` was 13 s on a warm Next.js cache and ~30 s cold (of which ~21 s
is the TypeScript pass), and `meiva start` paid it on every start — including a
`meiva stop`/`meiva start` cycle that changed nothing. It was the dominant cost
of a start that had no work to do.

## The key

`build_key` hashes:

- `source_digest(frontend_dir)` — HEAD plus every tracked or untracked,
  non-ignored file under `ui/web`. Gitignored outputs (`.next`,
  `node_modules`, `*.tsbuildinfo`, `next-env.d.ts`) are outside it by
  construction, so a build does not invalidate its own key.
- `node --version`.
- the `NEXT_PUBLIC_*` values baked into the bundle (`gateway_port`,
  `browser_origin`).

The key is recorded at `ui/web/.next/meiva-build-key` **after** a successful
build. A start whose key matches the recorded one skips the build; every other
start builds and then records.

## Why the reuse is safe

- **A failed or interrupted build cannot look current.** The key is written only
  after `npm run build` exits zero. An interrupted rebuild still carries the
  previous key, whose inputs differ from the current ones by construction.
- **Deleting the build deletes the claim.** The key file lives inside `.next`.
- **An unprovable key builds.** A checkout git cannot read (`source_digest`
  raises), or a missing `node`, yields `None` — and `None` always builds.
- **A node_modules that drifted from the lockfile is caught before the build
  decision**: `ensure_frontend_deps` still runs first and reinstalls on a lock
  change or drift, and the lockfile is part of the digest.

## Forcing a rebuild

Delete `ui/web/.next` (or just `ui/web/.next/meiva-build-key`). The next start
rebuilds.

## Boundaries

- Nothing here skips a *correctness* gate: the type check, the bundle build and
  the `exec` on the serve stage all still run whenever a build runs.
- The command shape stays a single compound whose final stage execs, so
  `base.sessions.env_forwarding.exec_into` keeps accepting it.
