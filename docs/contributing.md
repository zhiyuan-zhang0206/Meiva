# Contributing to Meiva

This is the entry point for coding agents contributing to Meiva. Start from the
user's goal and authorized scope. Inspect existing behavior before editing;
discuss unresolved design, contract or dependency choices with the user. Follow
[AGENTS.md](../AGENTS.md) for project invariants.

## Find the owner

Read the relevant component's local `docs/` and the [OKF index](index.meiva.okf.md).
Use `scripts/audit/where_used.py` before changing a symbol or contract so the
change includes real consumers, tests, patch targets and current documentation.
Cross-cutting rules live in [conventions](conventions/README.md); historical
[decisions](decisions/README.md) and [incident analyses](postmortems/README.md)
explain past choices and escapes rather than imposing today's workflow.

## Prepare an isolated checkout

Use a development clone and a branch from current `main`. Preserve existing work.
The helper `bash scripts/setup-worktree.sh <task>` prepares a worktree, locked
dependencies and its own real `.venv`; follow its final `worktree ready:` path.
A fork or another isolated Git setup is also valid when it preserves the same
environment boundaries. Do not edit a running production checkout or share its
virtualenv. See [development setup](conventions/dev-setup.md) and
[Git hook setup](conventions/runbook.md#git-hooks-pre-commit--pre-push).

Pytest isolates its environment. Other development tools importing application
code must set a temporary `MEIVA_HOME` before import; unset `MEIVA_HOME` selects the
default home and can target a running deployment.

## Change and verify

Implement a focused change with its related tests and current documentation.
**The change record is the PR.** The merge queue squashes a PR into one commit on `main`
whose title is the PR title and whose message is the PR description. That commit is the entry
a reader meets in `git log`, in the release notes page generated from a commit range
(`gh compare`), and in the commit `git blame` lands on, so the description is the permanent
record: write it for whoever reads it months later (what changed, for whom, the accounting of any
debt, the behavior that changes and how it was verified) and keep it true when the branch
changes. The commit messages inside the branch do not reach `main`, and a branch holds no
merge commit. There is no changelog file to append to and no per-entry file to add.

Since 2026-10-27 the queue squashes — squash is the only merge method now — and the
landed message is the description with its prose re-wrapped at 72 columns (a table
row or another block line keeps its own length), under a subject that is the title
with ` (#<n>)` appended. Before that date the queue rebased, so each commit there is
one landed change carrying its own message. Read both when you trace history; write
for the new one. `git commit --amend` no longer writes the record: it only rewrites a
local commit that never lands.

A `Co-authored-by: Meiva #<id>` line in the description lands as written. GitHub also
appends the `Co-Authored-By:` trailers whose value parses as `Name <email>`, taken from
the branch's commits; a trailer with no email, like the `Meiva #<id>` convention, is
not synthesized, so write that one in the description or it does not reach `main`.

```bash
# The description is the body of the landed commit: an empty one leaves no record.
test -n "$(gh pr view --json body -q .body)" \
  || echo "the PR description becomes the commit body on main: write it"

# The message the queue will put on `main`: subject, blank line, description.
gh pr view --json number,title,body -q '"\(.title) (#\(.number))\n\n\(.body)"'

# After it lands, that same text read back from the commit it produced.
gh pr view --json mergeCommit -q .mergeCommit.oid | xargs -r git log -1 --format=%B
```

Keep shared facts in their owning module; use existing third-party capabilities
rather than creating a second framework or registry. Resolve generated-file
conflicts by regenerating from the source, not editing derived output.

Run affected test files and relevant contract consumers. The [testing guide](conventions/testing.md)
has Python, frontend and e2e commands, environment details and cleanup guidance.
Full suites run in CI. The [migration guide](../db/docs/migrations.md) covers
schema changes, immutable migration history and baseline verification.

### Diff hints and `Hint-Ack`

The commit-msg and pre-push hooks ask about the shapes that reviews keep finding on the
lines a commit adds: an unmediated clock or environment read, a callable with a default,
a test that subclasses production code, a deleted test and the others in the
[catalog](../scripts/lint/docs/diff-hints/catalog.meiva.okf.md). Each hint says why the shape is a
problem and gives the recipe in order of preference. Change the code, or answer in the
commit message with one line per site:

```text
Hint-Ack: service-locator cli/commands/x/runtime.py:42 -- composition root of the daemon
```

A path without a line answers every site of that id in the file, and a directory with a
trailing slash every site below it. Answer with the reason the site is right, not with the
rule's name; an unanswered hint also stops the push, whichever commit carries the answer.

### Locality gate (meizel)

`python -m meizel check` judges every first-party dependency against the
`meizel.toml` manifests (see [the checker](../meizel/docs/checker/checker.meiva.okf.md)).
A commit runs `check --changed` on the staged files, a push and CI run the whole
`check`, and CI also compares recorded debt with the base branch.

Directories that still carry recorded debt are strict-to-be: the recorded
violations pass, anything new fails, and the debt may only shrink
([recorded debt](../meizel/docs/checker/debt.meiva.okf.md)).

- Fix a violation instead of recording it. Move the code, use the public door,
  inject the collaborator, or declare the edge in the manifest of the directory
  where the two sides meet.
- After a rebase or a move, run `python -m meizel check`, then `python -m meizel
  sync --declare` for `undeclared-edge` and `not-exported` (it declares only the
  legal new dependencies of your changed files and prunes stale ones). An edge
  that closes a cycle, a private name, an underscore module or a restricted form
  is not declared; change the code. Run `python -m meizel debt prune` for
  `debt-paid`. After moving a file, stage the move and run `python -m meizel debt
  carry` so its recorded debt follows it (it relocates entries, never adds one).
- `python -m meizel debt record` is for a deliberate exception that has been
  discussed with the maintainers and is explained in the PR. It is never the way
  to make a failing check pass.
- When a change pays debt down, run `python -m meizel debt prune` in the same
  change so the baseline drops with the fix. `python -m meizel debt` shows what
  is left.
- Recorded debt is derived from the tree, so a merge conflict in a `[debt.*]` table is not
  resolved by choosing a side or by hand: run `python -m meizel debt resolve <meizel.toml>...`
  (the manifests `git diff --name-only --diff-filter=U` lists), then `git add` them. It keeps the
  entries of both sides (the lower count where both changed) and lowers them to what still occurs.
  A conflict anywhere else in a manifest (purity, roots, uses, exports, state) is a decision for you:
  the command refuses it and writes nothing. If a violation is left that neither side recorded, it
  is a real new one; fix it, or `debt record` it as a discussed exception.
- CI fails when the recorded total grows against the base (`debt diff --on-growth=fail`).
  The one exception is a rule change (it edits meizel's own rule code: Python under `meizel/`
  outside tests, or `meizel.policy.toml`, whose reader tables decide what is an acquisition),
  whose growth is split in two by the tool, not by a reader. The growth the new rules cause in
  the base's unchanged code is reported, because widening a rule grows the baselines. The growth the content causes is failed: every debt key the change records must be one
  the new rules find in the base, so a rule change cannot carry a new violation or a moved file.
  Give a rule change its own PR. A queued comparison that includes it applies the same key check
  to the combined change; comparisons without rule changes are judged by totals as before.
- Patching is debt of every kind: `first-party-patch`, `stdlib-patch`, `third-party-patch`,
  `env-patch` and `unresolved-patch` (a target the imports do not resolve), and the two
  patch-shaped codes `patch-raising-false` and `test-subclass-override` (a test class that
  overrides a method of production code). Do not add one;
  pass the dependency in as an explicit parameter
  ([patching](../meizel/docs/checker/patching.meiva.okf.md)). When a symbol is renamed, `debt carry
  --rename-target OLD=NEW` moves its entries; when a function or class moves to another file,
  `debt carry --move-symbol OLD_FILE::NAME=NEW_FILE` does. `debt sites --code <patch code>
  --target-prefix <prefix>` lists where a target is patched (file, line, form), and `debt diff
  --by-target` reports what a change paid down per target; a codemod PR pastes the latter.
- Only a composition root reads the world. A file that takes `settings`, the environment, the
  clock, the home or working directory, the platform, a process id, randomness, or the process's
  own database, event bus or service endpoints is an `ambient-read` unless its directory's
  `meizel.toml` declares it under `[roots]` with that kind. Pass the value in from the root; a
  decision in the middle becomes a pure function of its inputs. Declare a file a root only when
  it is one, and a root that stops reading a kind fails as `stale-root`. A function that
  returns what the world decides (`meiva_home()`, `process_environment()`)
  and the process-wide object that does (`SYSTEM_TIMERS`, `Clock`) are listed in
  `meizel.policy.toml` as readers, and using one is a read of its kind; an entry whose
  definition moved fails as `stale-reader`, so a PR that moves or renames one updates the entry
  ([ambient reads](../meizel/docs/checker/ambient.meiva.okf.md),
  [readers](../meizel/docs/checker/ambient-readers.meiva.okf.md)).
- `meizel.policy.toml` lists every allowance a manifest cannot grant itself (`[external]` sites,
  `[loads]` sites, default-argument injections) and every exemption of the lint rules in
  `meizel/` (ambient state, clock lattice, single owners, setenv in tests, per-agent settings,
  `TYPE_CHECKING`) and the names through which the repository's own state is reached
  (`[ambient]`, `[ambient-readers]`). Rule code holds no exemption of its own. Do not edit the
  file unless the task names the entry; CI prints every change to it
  ([the policy file](../meizel/docs/checker/policy.meiva.okf.md)).

Before submitting, review your own diff with
[review-contribution](../.agents/skills/review-contribution/SKILL.md). It routes
to current project and domain rules. Record the findings and their resolutions in
the PR description. CI holds the mechanical gates; no second reader, approval
comment or signed report is required. If the review exposes an unresolved design
choice, discuss it with the user before changing that choice.

## Submit the PR

Push the branch and open a PR against `main`. Explain the problem, resulting
behavior, validation and known gaps. The description becomes the squash commit's
message and the title its subject, so both are the change note later readers and the
release notes see; write them as one (see [the change record](#change-and-verify)). Use
a file tree or control-flow explanation when it helps readers understand a larger
change; no fixed template is required.
Address relevant CI failures and review feedback. Distinguish successful execution
from missing, skipped or incomplete verification. The queue squashes the PR once the checks pass.

Keep the checkout while it contains needed work. Before removing a worktree,
check for live sessions or processes anchored there with
`scripts/host_ops/check_worktree_remove.py`; preserve it if the check cannot establish that
removal is safe. See [development setup](conventions/dev-setup.md).

A merge proves repository integration, not production health. Runtime rollout is
a separately authorized operator action with compatibility and recovery checks;
see [kernel change and deployment safety](../.agents/skills/meiva-self-development/SKILL.md).
