---
type: doc
title: Diff hints
description: The question layer between hard gates and free review - keyword-triggered hints on the lines a commit adds, each with its why and a recipe, answered by changing the code or by a Hint-Ack trailer; the catalog, the trailer format, the scope rules and the measured noise.
tags:
- scripts
- lint
- hooks
- review
---

# Diff hints

Review of the first refactor rounds found that about half of the defects in a large pull
request are a shape the diff shows: a test subclasses production code to override one
method, a callable parameter carries a default, a `case _:` swallows an unknown member, a
`Protocol` is defined above `base/`, a test is deleted without a word. The same shapes came
back in every round, and a fix often re-spelled the shape (`vars()` became a subclass, the
subclass became an armed flag). Hints put a question at the moment the shape is written.

## Three layers

| Layer | Standard | Where |
|---|---|---|
| Hard rule | deterministic, zero false positives, recorded debt may only shrink | `meizel check` (the patch codes, `missing-patch-target`, purity, locality) |
| Diff hint | keyword and AST triggered on added lines, false positives allowed, **must be answered** | this page |
| Review | equivalence of behavior, undeclared changes, honesty of the report | people |

A hint never fails because the code is wrong. It fails because nobody has said the code is
right. The answer is to change the code or to write one line in the commit message.

## Answering: the `Hint-Ack` trailer

```text
Hint-Ack: <hint-id> <path>[:<line>] -- <reason>
```

- `<path>:<line>` answers that one site; `<path>` answers every site of that id in the
  file; a directory (`cli/commands/lifecycle/`, with the trailing slash) answers every
  site of that id below it. A refactor that touches fifty test files answers `deleted-test`
  once for the directory, with the reason.
- The reason is a sentence about why the site is right (`composition root of the daemon`,
  `shell loop; the decision is unmet_step`), at least eight characters; the template
  placeholder `<reason>` is refused.
- The key is case-insensitive, like any git trailer key. A `Hint-Ack:` line in the final
  paragraph (the trailer block) that does not parse, names an unknown id or has no reason is
  an error that says how to fix it, not a silent non-answer; the same text earlier in the
  message is narrative and is only noted. An answer that matches no open site is reported and
  ignored.
- Source files carry no inline marker. A directory manifest's `settings` root declaration
  exempts `service-locator`; another kind cannot answer that hint. Roots and Python are read
  from the same Git revision (the index at commit-msg), so an unstaged declaration cannot
  answer staged code. The legacy filename heuristic remains: `main.py`, `__main__.py`,
  `runtime.py` and `app.py` are taken for composition roots. Every other site is answered.

## When it runs

| Stage | Command | What it checks |
|---|---|---|
| `commit-msg` | `hints.py commit-msg MSGFILE` | the staged diff (`git diff --cached -U0`) against the trailers of this message; line numbers are exact |
| `pre-push` | `hints.py branch` | the whole branch diff (`merge-base(origin/main, HEAD)..HEAD`) against the trailers of every commit on the branch |
| CI, pull requests | `hints.py branch --base BASE --head HEAD --summary FILE` | the same check over the pull request's own commits, and a table of every answer in the job summary |

The pre-push check closes what the commit check cannot see: an amend, a rebase and a
cherry-pick that never ran the commit-msg hook. It judges the final diff, so a hint that a
later commit removed is not asked. In a trailer a line number is only reliable in the commit
that wrote it (a later commit moves lines), so the branch check matches by id and path.
`hints.py report BASE HEAD [--json]` lists every site of a range without asking for answers;
it is how the calibration numbers were produced.

`default_install_hook_types` in `.pre-commit-config.yaml` makes a plain `pre-commit install`
(from the main clone, see [Git hooks](../../../../docs/conventions/runbook.md#git-hooks-pre-commit--pre-push))
install the `commit-msg` hook too, and `check_git_hooks.py` reports a clone that lacks it.

The CI job `diff hints` is the backstop for `--no-verify` and for a clone without the hooks.
It runs the pre-push check on the pull request's commits and writes every answer into the job
summary for the reviewer: the commit, the scope, how many sites it covers and the reason. A
directory trailer, or one that covers ten sites or more, is marked broad, so a reason that has
to carry a whole directory is read as such. The reason's length is not judged by the tool; its
visibility is the control.

A rename is a rename: the diff is read with rename detection, so a moved file reports nothing
and a moved file with edits reports its edits only. The diff prefixes are fixed to `a/` and
`b/` whatever `diff.mnemonicPrefix` or `diff.noprefix` say.

## What is read

- Python files, plus the `[roots]` table of every changed `meizel.toml` (a root added there is asked
  about); `git diff -U0` gives the added lines and, for `deleted-test`, the
  removed ones. A hint fires only for a node that starts on an added line (a function or
  class whose span holds an added line is looked at, but the site is the added line).
- Production hints judge the application and template packages from the root `meizel.toml`
  `[roles]` table. Test hints judge the tests of those packages and the top-level `tests/`.
  Tooling (`meizel/`, `scripts/`), examples and the frontend are outside both.
- A test file is any file below a `tests/` directory, a `conftest.py`, `test_*.py` or
  `*_test.py`. `time.sleep` and `os.environ` writes are only judged in `test_*.py` modules:
  a helper script run as a child process is the real resource those tests use.
- Names resolve through the file's own imports (`from time import monotonic as tick` is
  `time.monotonic`); a name that is not imported is not resolved.

## Where the rest is

- [[scripts/lint/docs/diff-hints/catalog.meiva.okf.md|The catalog]]: every id, what it fires on,
  the recipe it prints, the shapes it cannot see, and how to add a hint.
- [[scripts/lint/docs/diff-hints/calibration.meiva.okf.md|Calibration]]: recall and noise over
  the five pull requests the catalog came from, what the sample changed, and the cost of a commit.

Parent: [[scripts/lint/docs/lint.meiva.okf.md|lint]].
