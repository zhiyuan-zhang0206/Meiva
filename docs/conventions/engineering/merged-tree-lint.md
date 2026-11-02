# merged-tree-lint

## What this is now

Nothing runs. `merged-tree-structure` was removed from `.github/workflows/ci.yml` on 2026-11-01
(`#8091`), in the same window that retired the Trunk merge queue (`#8081`). There is no separate
merged-tree lint job, and no artifact by that name to download.

The violation class it watched for is still checked, on every pull request: CI checks out
`github.sha`, which for a `pull_request` event is `refs/pull/N/merge` — the head merged into the
base of that moment — and `backend-structure` runs the structural pre-commit gates against that
checkout. A file that only crosses the 800-line ceiling once `main`'s change and the pull
request's change are together is therefore visible in the ordinary required job.

What is **not** covered is the layer this job never covered either: the base can advance after the
run, and nothing re-tests the combination (`strict = false`). The current picture, including what
finds a merge-order problem after the fact, is in
[what verifies a merge](test-selection.md#what-verifies-a-merge).

## Why it existed

Issue #1584 exposed a merge-emergent violation. A file was 738 lines on main, then a PR added 75
lines, producing an 813-line file only in the Trunk combined tree. `lint-code-structure` enforces
an 800-line hard ceiling (`scripts/lint/code_structure.py` sets `_HARD_CEILING = 800`), and the
`lint-code-structure` pre-commit hook runs that check.

The PR head passed its own checks because it was tested against a stale main. The red result
appeared only after queue admission, which wasted a combined-tree round, bisection and re-review.
The job made that kind of violation visible while the PR was still being reviewed.

## How it worked (history)

It was an informational job: a finding did not block the workflow. It checked out the pull request
head with full history and merged `origin/main` into it with `git merge --no-commit --no-ff`, a real
three-way merge staged in the index with no commit and no branch refs. When the head already
contained `origin/main` there was no separate combined tree, and the job recorded `unchanged` and
skipped the lint run. The restore step ran unconditionally (`git reset --hard HEAD` and
`git clean -fd`) so the runner ended back at the head.

With the merged tree staged in the real index, `pre-commit run --all-files` enumerated exactly the
merged tree, with a `SKIP` list excluding hooks owned by other CI jobs. A merge conflict was not a
failed lint: the job recorded the conflicted paths and skipped the lint run.

Its own promotion criteria — observed informational runs for an agreed number of weeks and queue
cycles, recorded false positives, an agreed artifact workflow, and branch protection updated to
require the check — were never met. The job was removed with the queue instead, because the merge
ref checkout had made it a duplicate of the required structure job.

## Related

- [Backend test selection](test-selection.md) — who runs the full suite, and when.
- [Release train and channels](../../decisions/engineering/tooling/2026-11-01-release-train-and-channels.md)
  — why the queue was retired.
