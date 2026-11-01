# pnpm hoisted Copy-on-Write clones for frontend dependencies

## Context

Every Meiva worktree (`scripts/setup-worktree.sh`) and nine CI jobs materialize
`ui/web/node_modules` (~665 MB across ~49k files). Under `npm ci` against
`ui/web/package-lock.json`, each worktree unpacked tarballs anew into its own physical
inodes, spending ~30-60s of wall time and ~665 MB of unique disk blocks per concurrent
worktree, while CI cached npm's tarball cache (`~/.npm`) and re-extracted tarballs on
every shard.

Replacing `npm ci` with custom shell logic to copy or symlink another checkout's
`node_modules` reproduces the exact failure class already banned for `.venv`
([`docs/conventions/dev-setup.md`](../../../conventions/dev-setup.md#development-in-a-worktree)):
Next.js 16 Turbopack refuses external symlinks outside the project root unless
`turbopack.root` is widened, TypeScript `realpath` resolution follows symlinks back to
the donor checkout's tree, and postinstall mutations in one worktree corrupt another.
This gate chooses the mature package-manager equivalent of `uv`'s content-addressed
cache ([`docs/conventions/technology-selection.md`](../../../conventions/technology-selection.md)).

## Decision

Adopt `pnpm` (pinned at `11.9.0` in `base/host/brew_pin.py`, `ui/web/package.json`,
`scripts/provision/node.sh`, and CI's `pnpm/action-setup`) as the single frontend
package manager, configured in `ui/web/pnpm-workspace.yaml` with:

- `nodeLinker: hoisted`
- `packageImportMethod: clone-or-copy`
- `strictPeerDependencies: false`
- `allowBuilds` explicitly disabling untrusted native lifecycle scripts (`@parcel/watcher: false`, `@swc/core: false`)

`ui/web/pnpm-lock.yaml` (`lockfileVersion: '9.0'`) replaces `ui/web/package-lock.json`
and `ui/web/.npmrc`. Notice that `npm` and `npx` remain provisioned alongside `pnpm`
on hosts because MCP servers (`npx -y ...`) and Node itself still ship and consume them.

The five questions:

1. **Boundary.** `pnpm` owns resolution, lockfile integrity (`pnpm-lock.yaml`), the
   host-global content-addressed store, and materializing `ui/web/node_modules` via
   APFS Copy-on-Write `clonefile(2)` on macOS and hardlinks/copies on Linux, plus
   `pnpm audit --json` advisory reporting. Meiva owns the version pin
   (`base/host/brew_pin.py:PNPM_VERSION`), canonical-registry lockfile linting
   (`scripts/lint/locks/package_lock.py`), `meizel`'s exact TypeScript compiler lock
   verification (`meizel/language/typescript_compiler/compiler.mjs`,
   `meizel/source_evidence/loader.py`), and production converge drift detection
   (`cli/commands/_repo.py:_node_modules_drift`).
2. **Prior art.** Sharing JavaScript package files across checkouts without cross-tree
   symlinks is a solved problem at rung 3 of the ladder (`pnpm` content-addressed store
   with `nodeLinker: hoisted` + `packageImportMethod: clone-or-copy`, paralleling `uv`'s
   reflink/hardlink wheel cache for Python). Empirical measurement on this repository's
   `ui/web` tree (`pnpm 11.9.0`, macOS APFS):
   - Zero symlinks pointing outside `ui/web` (`EXTERNAL_SYMLINKS=0`);
   - `ui/web/node_modules/typescript/lib/typescript.js` (`9,144,216` logical bytes)
     consumes **`0` APFS private bytes** (`ATTR_CMNEXT_PRIVATESIZE = 0`);
   - Warm `rm -rf node_modules && pnpm install --frozen-lockfile` completes in `< 8s`
     combined (`~1-3s` for the install step alone).
3. **Simplest option that works.** One pinned CLI (`pnpm@11.9.0`), one project config
   file (`ui/web/pnpm-workspace.yaml`), one lockfile (`ui/web/pnpm-lock.yaml`), and
   `pnpm/action-setup` store caching in GitHub Actions. Zero custom worktree caching
   scripts, zero Next.js `turbopack.root` workarounds, and zero changes to existing
   flat `node_modules/<pkg>` consumers (`meizel`, TypeScript, ESLint, Vitest, Next.js).
4. **Failure prevented, and what we now handle ourselves.** Prevented: `O(N)` disk and
   extraction cost across `N` worktrees; cross-worktree symlink leakage into Turbopack
   and TypeScript; and silent lockfile bypass when `pnpm` previously ran against an
   `npm`-locked directory (postmortem 0007). Handled by us:
   - `pnpm` v11 ignores `.npmrc` settings and `npm_config_registry` in favor of
     `pnpm-workspace.yaml` and `pnpm_config_registry`: `scripts/lint/locks/package_lock.py`
     rejects reintroduced `package-lock.json` and `.npmrc` files and any `*registry*`
     setting in `pnpm-workspace.yaml`, while `scripts/mirrors/cn.env` exports both
     `pnpm_config_registry` and `npm_config_registry`.
   - `pnpm` v11 fails closed (`ERR_PNPM_IGNORED_BUILDS`) when dependencies declare build
     scripts not listed in `allowBuilds`: `pnpm-workspace.yaml` records explicit boolean
     decisions so newly added native-script dependencies surface at lock update time
     rather than running arbitrary install code or failing silently.
   - `pnpm run start` forwards `--` literally to `next start`: all callers pass
     `pnpm run start -p <port>` without `--`.
   - Converge's `_node_modules_drift` requires `node_modules/.modules.yaml`, rejects
     foreign `.package-lock.json` / `.yarn-integrity` markers, and checks direct
     dependency versions against `importers['.']` in `pnpm-lock.yaml`.
5. **Limit signal and exit.** Revisit if `pnpm`'s hoisted linker is deprecated upstream,
   if `pnpm` store cloning regresses on APFS/ext4, or if workspace-level strict
   isolation becomes desirable once direct consumers of `ui/web/node_modules/<pkg>`
   (`meizel`'s TypeScript loader) resolve through package entrypoints instead. Exit
   within `pnpm` is removing `nodeLinker: hoisted` in `pnpm-workspace.yaml`; exit off
   `pnpm` is `npm ci` via `pnpm`'s standard `package.json` specifiers (one lockfile
   regeneration, no application source changes).

## Alternatives rejected

- **Keep `npm ci` and copy/clone `node_modules` in `scripts/setup-worktree.sh`.** Rung-4
  shell caching: requires maintaining a donor checkout's freshness, breaks when the donor
  is mid-edit or on another branch, and helps local worktrees only (not CI).
- **`pnpm` default symlinked `node_modules/.pnpm` layout.** Introduces deep internal
  symlink farms and stricter phantom-dependency visibility changes at the same time as
  the package-manager swap; `nodeLinker: hoisted` isolates the storage/transport win
  without changing module resolution semantics.
- **Yarn Berry / Bun.** Yarn PnP breaks standard Node filesystem consumers (`meizel`);
  Bun introduces a second JS runtime outside Meiva's supported Node 22 baseline.

## Consequences

- Every host running the frontend service or contributor worktree bootstrap requires
  `pnpm` on `PATH` (`scripts/provision/node.sh` installs `pnpm@11.9.0` idempotently;
  `meiva start` host-dependency preflight checks `pnpm` alongside `node`, `npm`, `npx`).
- `scripts/ci/dependency_audit.py` audits `ui/web` via `pnpm audit --json` against
  `https://registry.npmjs.org` and tracks `PNPM_VERSION` against `pnpm/pnpm` GitHub
  releases alongside `UV_VERSION`.
