# Host versioning — derived from the commit, no bump discipline

Meiva's host version is the date axis of the running build, derived from the
checkout's commit — never a hand-maintained number. This page is the policy
the content-channel gates compare against (task #2915 design §5.5; landed with
tasks #2915 / #3267).

## The version

- **Bare form:** `YYYY.M.D` from the commit date of the checkout's HEAD
  (`base/host/git/host_version.py:host_version`) — e.g. `2026.9.13`. This is the
  string every gate compares, so it always exists on every checkout-mode
  machine, advances by itself, and needs no release process.
- **Display form:** `YYYY.M.D+g<short-sha>` (`host_version_display`) — for
  humans only: `meiva packages status`, log lines. Gates never see the suffix.
- **Fallback:** a checkout-less runtime (wheel install) falls back to
  `[project].version` in `pyproject.toml`, the repository's single product
  version, bumped only at a release milestone. It never selects the gate version
  in a checkout; it survives so the gate still has a value off a git checkout.
  When neither exists the caller surfaces "unknown" — never a guessed number a
  gate would trust.

## Release eligibility: timestamped CalVer tags carry `meiva-release/v1` certificates

The host version above answers "which tree is this machine running". A **release version**
answers a different question: "did this tree pass the release gate, and which channel promotes
it". The two never mix, and neither replaces the other; see the
[release-train and channel decision](../../decisions/engineering/tooling/2026-11-01-release-train-and-channels.md).

- **`vYYYY.MM.DD.HHMMSS[-hf<N>]` ⟺ the commit passed the release gate.** Every certified
  release is an annotated Git tag minted from the exact UTC second of certification (for example
  `v2026.11.01.183045`), carrying an embedded `meiva-release/v1` JSON block
  (`---BEGIN MEIVA RELEASE CERTIFICATE---` ... `---END MEIVA RELEASE CERTIFICATE---`) verified by
  `cli/fleet_update/plan/release_target.py:parse_tag_certificate` and written by
  `scripts/host_ops/release/certify.py`. Lightweight tags, forged CalVer tags without a valid
  certificate block, or tags whose peeled commit disagrees with the certificate's 40-hex `sha`
  are rejected. No `release/<version>.json` files or reverse commits are written to `main`.
- **Post-release hotfix suffix (`-hf<N>`).** Forward fixes on `main` mint the next timestamped
  `vYYYY.MM.DD.HHMMSS` tag on the Release Train. Out-of-band hotfixes that branch from an earlier
  certified release commit (`hotfix_base != latest_head`) append `-hf1`, `-hf2`, ... in strictly
  monotonic order with `kind = "hotfix"` and `hotfix_of` naming the base release tag.
- **Moving channel tags (`channel/latest`, `channel/stable`).** Two moving annotated Git tags
  embed a `meiva-channel/v1` JSON pointer (`---BEGIN MEIVA CHANNEL POINTER---` ...
  `---END MEIVA CHANNEL POINTER---`). `.github/workflows/release-gate.yml` (6-hour scheduled
  Release Train plus manual/hotfix dispatch) advances `channel/latest` and publishes the GitHub
  Release (`--latest=false`). `.github/workflows/promote-release.yml` invokes
  `scripts/host_ops/release/promote.py` to verify the target tag's `meiva-release/v1` certificate
  before advancing `channel/stable` and marking that GitHub Release `--latest`.
- **Enforced deployment gate (`cli.fleet_update`).** `cli.fleet_update` (`down` and `run`)
  resolves its target through `cli/fleet_update/plan/release_target.py:resolve_update_target`
  before opening any SSH session: `--channel stable|latest`, a certified `--release <tag>` or
  `--new <sha>`, or explicit preview opt-in `--track-branch <branch> [--new <sha>]` (which prints
  `target: UNRELEASED branch <branch> @ <sha> (preview / rapid-iteration mode)`). Any uncertified
  or tampered tag, channel pointer, or commit SHA exits 2 before touching any host.
- **Ancestry & train regression isolation.** `scripts/host_ops/release/base.py` verifies whether a
  target commit descends from a certified release tag (`git merge-base --is-ancestor`). When a
  scheduled Release Train batch uncovers a cross-PR regression between `channel/latest` and `main`
  `HEAD`, `scripts/host_ops/release/bisect_train.py` isolates the first bad commit in
  `ceil(log2(K))` steps inside a temporary detached worktree.
- **Historical milestone tags & `[project].version`.** Historical tags (`v0.2-eval-driven`,
  `v0.3-control-plane`, ...) are narrative markers and never satisfy release certification.
  `[project].version` in `pyproject.toml` remains only the wheel-install fallback for checkout-less
  runtimes and is decoupled from release tags.

Two axes, two questions: `YYYY.M.D+g<sha>` says *what is running*, `vYYYY.MM.DD.HHMMSS[-hfN]` +
`channel/{latest,stable}` says *what is certified and promoted*.

## What packages declare

`meiva-plugin.json` (spec: [`plugin-spec-v2.md`](../extensions/plugin-spec-v2.md)) carries the
package's compatibility claims. Every Meiva plugin ships one; a skill package may carry the
same manifest beside its `SKILL.md`; plugin bundles keep the plugin-level manifest as the
single source of truth for their bundled skills.

- **`engines.meiva`** — a semver range (`>=A,<B`; either side unbounded),
  compared against the bare derived host version. Min-and-max bounds exist in
  the range algebra; declare a max only when you mean it — the date axis
  advances every day the repo moves.
- **`requires_commit`** — the exactness layer: a commit SHA the host must
  *contain* (`git merge-base --is-ancestor`). Use it when content needs a
  fresh kernel capability; point it at the merge that introduced the
  capability. Zero bump discipline, exact within a day.
- A manifest must declare **either** `engines.meiva` **or** `requires_commit`
  (both is fine). Core content that depends on a fresh kernel capability uses
  `requires_commit`; third-party content, which targets released Meiva
  versions, uses date-axis `engines.meiva` ranges.

## Where the gates fire

| Point | Check | On violation |
|---|---|---|
| install / upgrade (skill / plugin / mcp) | `engines` range + `requires_commit` vs this checkout | refuse, report (`cli/commands/extensions/_manifest_gate.py`) |
| content-channel refresh landing | same, against the staged tree | keep the current content, record `blocked_version`, retry after the host moves |
| runtime plugin load | `engines` vs the derived version | skip that plugin, loud report, process continues |
| runtime skill scan | `engines` vs the derived version | excluded from the catalog with a visible reason |

## No bump discipline

There is deliberately **no** "bump a version on content-facing merges" rule —
that was the rejected v2 proposal (a number nothing else maintains, enforced
by CI bookkeeping). The derived date advances by itself, and a first-class
human version, if one is wanted, is a separate release-process call, not a
dependency of this policy.

## CI

`scripts/content_lint/lint_core_content_manifests.py` (CI job `core-content manifests`,
pre-commit `lint-core-content-manifests`) keeps core content honest:

- every builtin plugin ships a manifest, every manifest under `meiva_builtins/`
  validates, its `engines` ranges admit the repo's current derived version, and
  any `requires_commit` is an ancestor of HEAD (a typo'd or future SHA is red);
- every other manifest in the tree is *audited* (report-only): its ranges are
  judged against the derived version.
