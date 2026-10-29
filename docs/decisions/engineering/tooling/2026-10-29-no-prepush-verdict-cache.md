# A passing commit stage does not stand for the push stage

## Context

`pre-push` re-runs every commit-stage hook over the branch contribution
(`scripts/hooks/prepush-branch-lint.sh`), and that nested run judges the pushed
commit in a scratch checkout of `HEAD`
([branch-diff scratch checkout](2026-10-17-branch-diff-scratch-checkout.md)).
One change is therefore checked twice: once against the commit, once against the
range the push carries.

Both stages are expensive on a shared host. Measured here (appendix): the push
stage took 69-167s (median 101s) over seven real merged ranges, and the commit
stage took 59.5-66.7s at load 8.7-13.8 and 100.2-116.6s at load 14.7-23.0 while
40 of 61 configured hooks executed. The largest single pre-push hook is the
nested re-run itself, at 94.65s on the branch measured there.

That invites reusing the commit stage: record the pass, and let the push skip what
it already judged. Task #5754 built exactly that (an exact-range ledger keyed by
parent tree, commit tree and hook-set version, plus a push-side gate, a wrapper
that wrote the record and an installer), and the review of that design produced
three mechanical counterexamples. This record states the decision that followed
and the boundary of the evidence, so the shape is not rebuilt from a key format.

## Decision

The push stage keeps re-running every commit-stage hook. Nothing in the hook path
records a stage verdict or reads one: no ledger, no marker, no wrapper.

The rejected claim is specific, not a claim that caching is impossible: **one
pass at the commit stage cannot stand for the push stage's execution.** A future
attempt starts from the differences below and from a construction that answers
each of them, and has to show that the construction fails when the two executions
differ. A record format is not a start.

## Why: the two stages are different executions

1. **Different command line.** The commit stage is invoked with the files being
   committed; the push stage is invoked with the range (`--from-ref`/`--to-ref`).
   A hook that reads `PRE_COMMIT_FROM_REF` or `PRE_COMMIT_TO_REF` - which
   `pre-commit` supplies to hooks with `always_run: true` and
   `pass_filenames: false` - sees nothing at commit and a range at push, so the
   same tree can pass one and fail the other.
2. **Different file set.** The commit stage runs in the developer's working tree,
   where an untracked file exists; the push stage runs in a scratch checkout of
   `HEAD`, where it does not. A hook that reads a generated or local path returns
   different verdicts for identical trees.
3. **Different judge.** `pre-commit` refuses an unstaged configuration, so a
   configuration change is staged before the commit-stage run, and `git commit
   --only PATH` then commits the named path alone while `HEAD` keeps the previous
   configuration. The run that passed used a hook set, arguments or thresholds the
   pushed tree does not contain, and the push re-judges `HEAD` with the committed
   configuration.
4. **Inputs outside the tree.** `check-git-hooks-install` reads this clone's
   installed hooks. The toolchain that runs the hooks lives in `.venv` and
   `ui/web/node_modules`. Hooks also read the clock and the network, and commit
   messages and refs are Git metadata rather than tree content. A key over trees
   cannot name any of them. An environment stripped with `env -i`, a pinned
   realpath and a declared offline mode do not stop network use, freeze the clock
   or hide files outside the repository: that construction remains to be proven,
   and this decision does not assume it exists.
5. **Per-commit passes do not compose into a range.** The push run judges the
   pushed range as one tree, so a pass at `(A, B)` and a pass at `(B, C)` say
   nothing about `(A, C)`. A record per commit is not a record for the range.

## Alternatives rejected

- **An exact-range ledger plus a push-side gate.** It keys on trees, so it cannot
  express points 3 and 4; a gate over it is consumed by a source that does not
  exist. Withdrawn with the prototype rather than left in place for a later
  writer who would read the gate as a working interface.
- **A marker written by the last commit-stage hook and promoted by `post-commit`.**
  `git commit --no-verify` skips `pre-commit` and `commit-msg` but still runs
  `post-commit`, so the marker can be written for a stage that never ran; a stage
  that fails after a hook already wrote its marker leaves a stale one.
- **Recording from `post-commit` alone.** The same `--no-verify` hole.
- **A time window or staleness heuristic.** It replaces the question "did this
  execution happen" with a guess about how recently a different execution did.
- **Recording only at push time.** The push is the first execution of that range,
  so there is nothing to reuse.
- **"Already-passing ranges agree" as equivalence evidence.** A replay corpus of
  ranges that already passed can only produce passes; it cannot bound the error.
  An experiment over 15 ranges and 23 commits (19 comparable, no counterexample)
  is a statement about that sample, not about equivalence.

## Consequences

- A push pays the full stage. A cache of this shape would have to save a repeated
  execution of a range, and the counterexamples above show that the repeat is not
  the same execution; how often a developer pushes a range that was already judged
  is unmeasured here, and is recorded as unknown rather than assumed to be rare or
  frequent.
- The cost that can be removed without changing what is judged sits inside the
  hooks. The two most expensive push-stage hooks are tracked separately and are
  not part of this decision.
- The numbers below are a cost record. Nothing in them shows a step that can be
  dropped while judging the same thing, and no saving from this decision has been
  implemented or measured.

## Appendix: measurements

### Push stage, per hook

Seven merged pull-request ranges (one to four commits each, covering docs,
scripts, frontend and runtime changes), each re-run with the command
`pre-commit run --hook-stage pre-commit --from-ref <base> --to-ref HEAD` - the
command the branch-diff hook itself issues. Durations in seconds.

| Hook | Ranges that ran it | Median | Min-max |
| --- | ---: | ---: | ---: |
| `check-code-comments` | 7/7 | 23.2 | 1.6-43.1 |
| `lint-host-platform-marks` | 7/7 | 15.7 | 13.6-27.5 |
| `frontend-eslint` | 1/7 | 11.5 | 11.5 |
| `meizel-check-changed` | 7/7 | 5.6 | 4.3-7.6 |
| `types-codegen-fresh` | 1/7 | 4.7 | 4.7 |
| `check-doc-references` | 5/7 | 3.3 | 3.1-5.9 |
| `lint-code-structure` | 7/7 | 1.8 | 1.0-54.1 |
| `lint-doc-symbols` | 1/7 | 1.7 | 1.7 |

The remaining 27 hooks each had a median at or below 1.2s. The medians sum to
83.7s against a whole push-stage median of 100.9s; the difference is hook startup
and scheduling between hooks.

| Ranges (commits) | Push stage | Sum of that branch's commit-stage runs |
| --- | ---: | ---: |
| 1 | 68.9 | 35.4 |
| 1 | 83.4 | 83.6 |
| 1 | 102.5 | 70.0 |
| 1 | 166.5 | 153.7 |
| 2 | 100.9 | 136.3 |
| 3 | 143.1 | 188.0 |
| 4 | 85.6 | 266.0 |

The second and third columns answer different questions: the push stage runs the
hook set once over the range, while the sum counts one whole commit-stage run per
commit on that branch. The comparison is not a repeat of the same command.

### Commit stage

40 of 61 configured hooks executed on the measured change; the rest matched no
file. Whole stage: 59.5s and 66.7s at load 8.7-13.8, and 100.2s, 112.7s and
116.6s at load 14.7-23.0.

### Method and limits

Ranges were rebuilt from `refs/pull/<n>/head`, with today's `main` as their base;
a base that moved only widens a range, never narrows it. Each range ran in a
detached worktree of that head with the checkout's `.venv` and
`ui/web/node_modules` linked in. Per-hook timings are serial re-runs of the same
command with warm caches, each minus the all-skipped startup (0.9-1.5s). The host
is shared and was under load 10-25 with other lanes running; these are medians of
seven ranges on one machine, not a distribution, an SLA or a hardware comparison.
Single outliers (54.1s for `lint-code-structure`) are not that hook's normal cost.

## Appendix: three minimal reproductions

Each block below is a complete script for an empty directory: it creates the
repository, the hook, its configuration and its inputs, so it can be pasted as-is
into a shell where `pre-commit` is on `PATH` (4.6.2 here) and run from start to
finish. No block depends on another, on this checkout or on a file outside it.
All three show the same thing: the stage a developer runs at commit and the stage
the push runs disagree about one commit.

### An untracked file

```sh
git init -q -b main
git config user.email probe@example.com
git config user.name probe
cat > probe.sh <<'EOF'
#!/bin/sh
[ -f local-only.txt ] || { echo "missing local-only.txt"; exit 1; }
EOF
chmod +x probe.sh
cat > .pre-commit-config.yaml <<'EOF'
default_stages: [pre-commit]
repos:
  - repo: local
    hooks:
      - id: probe
        name: needs local-only.txt
        entry: ./probe.sh
        language: system
        always_run: true
        pass_filenames: false
EOF
git add .pre-commit-config.yaml probe.sh
touch local-only.txt                             # local artifact, deliberately untracked
git ls-files --error-unmatch local-only.txt || true   # fails: not part of the tree
pre-commit run --hook-stage pre-commit           # Passed, rc=0
git commit -q -m base
git rev-parse HEAD^{tree}
scratch=$(mktemp -d)
git worktree add -q --detach "$scratch" HEAD
cd "$scratch" && git rev-parse HEAD^{tree} && pre-commit run --hook-stage pre-commit
```

Observed: `git ls-files --error-unmatch` reports that `local-only.txt` is not
known to git; the hook prints `Passed` with rc 0 in the working tree; both
`git rev-parse HEAD^{tree}` lines print the same tree hash; and in the checkout
of that same tree the hook prints `Failed`, `missing local-only.txt`, rc 1.

### A staged configuration the commit does not carry

```sh
git init -q -b main
git config user.email probe@example.com
git config user.name probe
cat > judge.sh <<'EOF'
#!/bin/sh
echo "judge ran"; exit 1
EOF
chmod +x judge.sh
printf 'one\n' > subject.txt
cat > .pre-commit-config.yaml <<'EOF'
default_stages: [pre-commit]
repos:
  - repo: local
    hooks:
      - id: judge
        name: judge
        entry: ./judge.sh
        language: system
        always_run: true
        pass_filenames: false
EOF
git add -A && git commit -q -m "base with the judging hook"
printf 'two\n' > subject.txt
printf 'repos: []\n' > .pre-commit-config.yaml   # remove the judging hook, not yet staged
pre-commit run --hook-stage pre-commit           # [ERROR] ... is unstaged, rc=1
git add .pre-commit-config.yaml subject.txt      # stage it; the run below now starts
pre-commit run --hook-stage pre-commit; echo "rc=$?"   # no hooks configured
git commit -q --only subject.txt -m "change subject only"
git show HEAD:.pre-commit-config.yaml            # still configures the judging hook
scratch=$(mktemp -d)
git worktree add -q --detach "$scratch" HEAD
cd "$scratch" && pre-commit run --hook-stage pre-commit   # Failed, rc=1
```

Observed: the first run refuses with `[ERROR] Your pre-commit configuration is
unstaged.` and rc 1 - that refusal is why the configuration has to be staged, and
why the run that follows uses a configuration the commit does not carry. After
staging, the same command prints `rc=0` and names no hook; `git show
HEAD:.pre-commit-config.yaml` still prints the judging hook; in the checkout of
`HEAD` that hook prints `judge ran` and fails with rc 1.

### A hook that needs the pushed range

```sh
git init -q -b main
git config user.email probe@example.com
git config user.name probe
cat > range.sh <<'EOF'
#!/bin/sh
echo "FROM=${PRE_COMMIT_FROM_REF-<unset>} TO=${PRE_COMMIT_TO_REF-<unset>}"
[ -z "${PRE_COMMIT_FROM_REF-}" ] || exit 1
EOF
chmod +x range.sh
cat > .pre-commit-config.yaml <<'EOF'
default_stages: [pre-commit]
repos:
  - repo: local
    hooks:
      - id: range-only
        name: range only
        entry: ./range.sh
        language: system
        always_run: true
        pass_filenames: false
EOF
git add -A && git commit -q -m one
git commit -q --allow-empty -m two
pre-commit run --hook-stage pre-commit                                  # Passed, rc=0
pre-commit run --hook-stage pre-commit --from-ref HEAD~1 --to-ref HEAD  # Failed, rc=1
```

Observed: without a range the hook passes with rc 0 - a hook that passes prints
nothing, so the unset refs are invisible there - and with the range it prints
`FROM=HEAD~1 TO=HEAD` and fails with rc 1.
