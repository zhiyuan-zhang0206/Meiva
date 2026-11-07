# Certified cluster releases stay off the GitHub Releases page

Supersedes the GitHub-Release steps of
[2026-11-01-release-train-and-channels.md](2026-11-01-release-train-and-channels.md): tag minting
(§1) and promotion (§4).

## Context

The 2026-11-01 decision made `release-gate.yml` publish a GitHub Release for every certified
version (`gh release create "$VERSION" --latest=false`) and `promote-release.yml` mark the promoted
one `--latest`. Both were meant as a human-facing mirror of the tag machinery. Two consequences
showed up within six days:

1. **Shell accumulation.** The train certifies `main` `HEAD` every six hours whenever it advanced,
   so the page grew one release per train: on 2026-11-07 it carried 21 `v2026.11.*` releases minted
   since 11-01, each a title, a commit SHA and a one-line note, none with an asset.
2. **A taken `Latest` badge.** A repository has one `Latest`, and promotion set it on the cluster
   release: on 2026-11-07 `channel/stable` pointed at `v2026.11.07.064419`, which was the
   repository's `Latest`, while `android-v1.0.1` — the artifact release users download — carried no
   badge.

Neither release object had a reader. Every consumer of certification reads Git, not the Releases
API: `scripts/host_ops/release/base.py:load_certified_releases` walks local tags through
`scripts/host_ops/release/git_tags.py:list_tag_names`; `cli/fleet_update/plan/release_target.py`
resolves `refs/tags/<version>` and `refs/tags/channel/{latest,stable}`;
`scripts/host_ops/release/{certify,promote}.py` mint and verify the `meiva-release/v1` certificate
inside the annotated tag message. The repository's only `repos/*/releases/latest` call is
`scripts/ci/dependency_audit.py`, and it reads third-party vendors (wal-g, uv, pnpm, Grafana), not
this repository. The app update chain uses fixed `app-latest` / `android-latest` asset endpoints
precisely because dated cluster releases made `/releases/latest` unusable as an app channel
(`.github/workflows/release-app.yml`).

## Decision

A certified cluster release publishes Git only: the annotated `vYYYY.MM.DD.HHMMSS[-hfN]` tag, the
`channel/latest` pointer, and on promotion the `channel/stable` pointer. `release-gate.yml` and
`promote-release.yml` create, edit and mark no GitHub Release. The Releases page belongs to artifact
deliverables — `android-v*`, `app-v*` and their `*-latest` feeds — and its single `Latest` badge
stays with them.

## Alternatives rejected

- **Keep the newest release and prune older ones on every train.** A prune step would run on every
  certification to delete objects nothing reads, and the survivor would still compete for the one
  `Latest` badge with the artifact release. It buys no reader, only hygiene of a page that still
  misleads.
- **Keep creating with `--latest=false` and stop marking `--latest` on promotion.** That is what
  produced the 21 shells: `--latest=false` says nothing about whether a release should exist at all.
- **Keep promotion's release as a browsable "stable history".** A release with no assets and one
  line of text is not history: `channel/stable` carries the promoted version, its SHA and the
  operator's reason, and `git tag -l 'v*'` lists every certified version with its certificate.

## Consequences

- The certify job publishes two Git refs and records the version in the run summary; the publish
  step keeps only the checkout credentials it needs for `git push`. `promote-release.yml` keeps
  pushing `channel/stable` and likewise writes the promoted version to the run summary.
- No automation removes old shells, because nothing creates them any more. The operator deleted the
  accumulated ones once on 2026-11-07 (27 releases -> 3: the two `android-v*` artifacts and the
  `android-latest` feed), deleting Release objects only and keeping every git tag, so certificate
  verification and `cli.fleet_update` were unaffected; `android-v1.0.1` holds `Latest` again.
- `tests/ui/test_release_gate_workflow.py::test_cluster_release_workflows_publish_no_github_release`
  fails if either workflow reintroduces `gh release`.
