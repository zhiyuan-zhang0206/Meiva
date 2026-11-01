# Post-merge release train, timestamped CalVer certificates, and channel-gated fleet updates

Supersedes [2026-10-03-delete-dated-release-tag-cadence.md](../../../runtime/updates/release/2026-10-03-delete-dated-release-tag-cadence.md).

## Context

Meiva previously combined three mechanisms that worked against fast, safe delivery:

1. **Pre-merge Trunk merge queue (`.trunk/trunk.yaml`).** Every pull request had to pass PR CI and then wait in a serialized `trunk-merge/*` queue running the full suite again before landing on `main`. Independent changes blocked behind unrelated queue heads, transient infrastructure hiccups evicted whole batches, and maintaining a second configuration source in `.trunk/trunk.yaml` duplicated GitHub's native branch-protection rules.
2. **Hand-bumped `vX.Y.Z` + `[project].version` lockstep + `release/<version>.json` reverse commits.** Cutting a release required bumping `pyproject.toml`, running `release-gate.yml`, and writing `release/<version>.json` back into the repository. Writing a certificate file into protected `main` required a reverse bot PR through the same merge gates, creating a chicken-and-egg loop whenever a hotfix or release cut was needed quickly.
3. **Unenforced deployment targets.** `cli.fleet_update` accepted any `--new <sha>` and checked it out across the fleet without verifying whether that commit had ever passed the release gate.

Earlier, [2026-10-03-delete-dated-release-tag-cadence.md](../../../runtime/updates/release/2026-10-03-delete-dated-release-tag-cadence.md) removed a dormant `release_cut.py` tagger because nothing consumed dated tags at deploy time. Now that `cli.fleet_update` enforces release certification before touching any host, release identity, certification storage, merge admission, promotion, and fleet rollout belong to one cohesive design.

## Decision

Adopt a unified five-part release and deployment architecture:

### 1. Timestamped UTC CalVer (`vYYYY.MM.DD.HHMMSS` / `-hf<N>`) and Git-native tag certificates

- Every release tag uses exact-second UTC CalVer: `vYYYY.MM.DD.HHMMSS` (for example `v2026.11.01.183045`), minted from the UTC timestamp when `release-gate.yml` certifies the commit. Same-day multiple releases collide neither chronologically nor lexicographically.
- Post-release hotfixes that branch from an earlier certified release commit (rather than `main` `HEAD`, so `hotfix_base != latest_head`) carry `-hf<N>` (`N >= 1`, strictly monotonic per base version, e.g. `v2026.11.01.183045-hf1`). Ordinary forward fixes on `main` simply mint the next timestamped `vYYYY.MM.DD.HHMMSS` release.
- Certification is stored **inside the annotated Git tag message** as a delimited `meiva-release/v1` JSON block (`---BEGIN MEIVA RELEASE CERTIFICATE---` ... `---END MEIVA RELEASE CERTIFICATE---`) binding `version`, full 40-hex `sha`, `kind` (`train` | `manual` | `hotfix`), `hotfix_of`, `certified_at`, `gate_workflow`, `gate_run_id`, `gate_run_url`, and `source_ref`.
- `release/<version>.json` files and reverse release PRs are eliminated. `[project].version` in `pyproject.toml` is decoupled from release tags; `base/host/git/host_version.py` (`YYYY.M.D` derived from commit date) remains unchanged as the compatibility axis for `meiva-plugin.json` `engines.meiva`.

### 2. Moving channel tags (`channel/latest`, `channel/stable`) and explicit `--track-branch`

- Two moving annotated Git tags represent release channels: `channel/latest` and `channel/stable`. Each embeds a `---BEGIN MEIVA CHANNEL POINTER---` `meiva-channel/v1` JSON block (`channel`, `version`, `sha`, `updated_at`, `actor`, `reason`) and peels directly to `sha`.
- Readers verify both sides: the `channel/<name>` tag must carry a valid `meiva-channel/v1` block whose `sha` equals the commit `refs/tags/channel/<name>` peels to, **and** the referenced `refs/tags/<version>` tag must exist in the repository with a valid `meiva-release/v1` certificate for that exact same `sha`.
- Rapid iteration on preview or development clusters uses an explicit `--track-branch <branch> [--new <ancestor-sha>]` mode rather than weakening certified channels.

### 3. Trunk merge queue retired in favor of fast PR gates, a 6-hour Release Train, and `O(log K)` bisect

- `.trunk/trunk.yaml` is deleted. Pull requests merge directly onto `main` once GitHub's required PR status checks (`ci.yml` with impact-selected backend shards) pass. `scripts/audit/branch_protection.py` owns the canonical required-check contract directly.
- `.github/workflows/release-gate.yml` runs as a scheduled **Release Train** every 6 hours (`cron: '17 */6 * * *'`) and on `workflow_dispatch`. On schedule, if `channel/latest` already points to `main` `HEAD`, `prepare` emits `skip_train=true` and all heavy verification jobs skip cleanly in seconds.
- When `main` has advanced (or on manual/hotfix dispatch), the Release Train executes the full verification suite: backend static + structure + all 8 backend shards with the 85% coverage gate, frontend (`eslint + tsc + vitest`), 4-shard Playwright e2e, hosted e2e, Linux container verification boundary, and release notes, then mints the `vYYYY.MM.DD.HHMMSS[-hfN]` certificate tag, advances `channel/latest`, and publishes a GitHub Release (`--latest=false`).
- If a batch of `K` commits since `channel/latest` exposes a cross-PR interaction on the train, `scripts/host_ops/release/bisect_train.py` isolates the exact first bad commit in `ceil(log2(K))` probes inside a temporary detached worktree without mutating the caller's checkout, mapping pytest exit `0 -> 0`, `1 -> 1`, and collection/setup errors (`2..5`) to `125` (`git bisect skip`).

### 4. Explicit `stable` promotion workflow (`promote-release.yml`)

- Scheduled trains and `main` merges never advance `channel/stable` automatically.
- `.github/workflows/promote-release.yml` (`workflow_dispatch` taking `version` and `reason`) runs `scripts/host_ops/release/promote.py`, which verifies the target tag's `meiva-release/v1` certificate before force-updating `refs/tags/channel/stable` and marking the corresponding GitHub Release `--latest`.
- Merging or promoting never deploys production; cluster rollout remains a separately authorized operator action.

### 5. Fail-closed target gate in `cli.fleet_update`

- `cli/fleet_update/plan/release_target.py` (pure stdlib decision core) and `cli/fleet_update/fleet_gate.py` require every `down` and `run` invocation to select exactly one target mode:
  1. `--channel stable|latest` (production standard: `--channel stable`),
  2. `--release vYYYY.MM.DD.HHMMSS[-hfN]` or `--new <40-hex-sha>` (must carry a verified `meiva-release/v1` certificate), or
  3. `--track-branch <branch> [--new <ancestor-sha>]` (verifies ancestry on `refs/remotes/origin/<branch>` and prints `target: UNRELEASED branch <branch> @ <sha> (preview / rapid-iteration mode)`).
- Lightweight tags, forged CalVer tags without a valid `meiva-release/v1` certificate, tampered channel pointers, and uncertified commit SHAs fail closed with exit code `2` **before** opening any SSH session, writing silence rules, or stopping any service.

## Alternatives rejected

- **Keep `release/<version>.json` files committed to `main`.** Storing release certificates as tracked files requires either pushing directly to protected `main` from Actions (bypassing branch protection) or opening a reverse pull request per release (re-triggering CI and breaking immutability of the certified commit SHA). Annotated Git tag objects live in the Git object database alongside commits, bind the exact commit SHA without creating a child commit, and travel via `git fetch --tags`.
- **Allow bare `--new <sha>` to deploy uncertified commits with a warning.** A warning does not prevent an accidental copy-paste of an uncertified `main` commit onto production. Requiring `--track-branch <branch>` for uncertified commits keeps production commands fail-closed while preserving friction-free preview iteration.
- **Keep Trunk merge queue alongside the Release Train.** Running the full test matrix both pre-merge in a serialized queue and post-merge on the Release Train doubles runner spend while keeping PR latency bound to queue depth. Impact-selected PR checks catch direct regressions pre-merge; the 6-hour full-suite train plus `O(log K)` automated bisect bounds cross-PR escape detection prior to `channel/latest` certification.

## Consequences

- Tag protection rules on GitHub should restrict `refs/tags/v*` creation (immutable) and `refs/tags/channel/*` updates to GitHub Actions (`github-actions[bot]`) and repository administrators.
- Production fleet updates use `--channel stable` (or `--release <tag>` for pinned rollbacks/rollouts); preview clusters iterating on `main` or feature branches pass `--track-branch <branch>`.
- `.trunk/` is removed from the repository, and weekly branch-protection audits compare live GitHub settings directly against `scripts/audit/analysis/protection.py`.
