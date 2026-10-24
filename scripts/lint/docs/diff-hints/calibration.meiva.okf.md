---
type: doc
title: Diff hints calibration
description: Recall and noise of the diff hints over the five pull requests whose reviews seeded the catalog, what the sampled hits changed, and what a commit pays.
tags:
- scripts
- lint
- hooks
---

# Diff hints calibration

The hints were run over the pull requests whose reviews are the source of the catalog:
\#6810, \#6813, \#6816, \#6846 and \#6848 (PR heads against their merge bases; \#6813 at the
head the review judged). That is 2,109 changed files. These are the pull requests the catalog
was fitted on, so the recall below is a fit, not a prediction; there is no hold-out set yet.
The first real one is the next set of reviewed pull requests, and this page is updated from it.

| Measure | Value |
|---|---|
| Sites raised | 670 (0.32 per changed file) |
| Answers needed at path level | 463, of which 261 are `deleted-test` (one per file; one per directory with a directory trailer) |
| Reviewed findings | 73 |
| Findings that are a diff shape | 38 (52%) |
| Of those, a hint fired on the cited site | 33 (87%) on the fitted set; of all 73 findings 45% |

Not caught although mechanical (5): a source-text assertion whose added lines carry no
`inspect.getsource`, a `Callable` type alias used as a required port, an enum if-chain without a
final `else`, `Literal` selectors that fall into `else`, and a field compared with a literal
(`fact.role == "shell"`, left to review on purpose). Of the 35 findings that are not a diff shape,
9 are leftovers in untouched code, 4 are patch bookkeeping that meizel owns, and 22 are behavior,
coverage quality or honesty of the report.

## Misfires, measured per id

A misfire is a site where the code does not have the shape the hint names (a detector error).
Whether a correct site is fine is the author's answer and is the point of asking, so it is not
counted here. Each id was read site by site, or by a random sample of 30 (20 for
`service-locator`) when it has more; nothing is extrapolated. `deleted-test` is outside the
count: every hit is a real removal and no detector decides it.

| Id | Sites | Files | Judged | Misfires | Findings hit | Note |
|---|---|---|---|---|---|---|
| `ambient-read` | 123 | 49 | 30 | 0 | 3 | 24 of the 30 are a `time.monotonic` read for a deadline, a duration or a TTL, answered in one line; 6 read the environment, home, platform, settings or the wall clock |
| `service-locator` | 102 | 34 | 20 | 0 | 1 | |
| `lambda-dependency` | 45 | 22 | 30 | 2 | 2 | `pipeline_factory=lambda: pipeline`, a single lazily built resource, which the conventions allow |
| `test-seam` | 33 | 20 | 33 | 0 | 3 | 18 are an armed flag or a value assigned on a fake's own fixture object, which the red line rules out and the reviews found |
| `mutable-in-frozen` | 33 | 23 | 33 | 0 | 2 | |
| `protocol-outside-base` | 27 | 15 | 27 | 0 | 4 | |
| `test-subclass` | 11 | 11 | 11 | 1 | 6 | a fake that implements `SessionBackend`, a Protocol the resolver cannot tell from a class |
| `wildcard-case` | 9 | 8 | 9 | 0 | 2 | |
| `none-fallback` | 8 | 6 | 8 | 1 | 1 | an optional path with a computed default |
| `any-collaborator` | 7 | 4 | 7 | 0 | 2 | |
| `callable-default` | 6 | 5 | 6 | 2 | 0 | `database_factory = never_dialed`, a default that raises |
| `callable-bag` | 3 | 3 | 3 | 1 | 1 | `WrapAdded`, the undo record of a wrap layer, not a dependency |
| `spy-assert` | 2 | 2 | 2 | 0 | 0 | |
| `deleted-test` | 261 | 261 | not judged | not judged | 6 | |

Pooled over the 219 judged sites: 7 misfires (3.2%). `callable-default` and `spy-assert` hit no
finding because the reviewed callbacks and spies were leftovers outside the added lines.

What the calibration changed, each from a sample of the hits:

- `any-collaborator` first matched every `value: object` of a parser (most of its 53 hits). It now
  needs a collaborator name; `Any` and `object` are both judged by it.
- `none-fallback` and `mutable-in-frozen` first fired in test builders and recorders (frozen
  `body: dict` payloads, `clock or SystemClock()` defaults); they judge production files only.
- `test-seam` first listed 63 `time.sleep` hits, nearly all child scripts, poll loops and the
  simulated slowness inside a fake server's handler. It now judges the sleep in the body of a
  `test_*` function only, and not one that paces a loop. `del sys.modules[..]` cleanup,
  `self.__dict__[..]` inside a double's own constructor and assignments on the real-resource
  fixtures (`db_conn`, `database`, `event_bus`, `pool`) are not seams.
- `test-subclass` first included every third-party base (`BaseChatModel`, `httpx.BaseTransport`:
  implementing an abstract interface); only database and cache clients remain, which is the
  `ParkedPool(AsyncConnectionPool)` case.
- `ambient-read` first counted a `datetime.now(UTC)` handed straight to a call as `now=`,
  `*_at` or `*_ts` (the shell reading once and passing the value, which is the form the hint
  asks for) and generated ids (`uuid4`); both are out. It stays broad on purpose: clock reads in
  a shell are the most common thing the red line moves into a root, and the answer is one line.
- A field compared with a literal was added to `wildcard-case` and removed again: 4 of its 17
  sites were on a reviewed finding, the rest compared wire kinds (`item.kind == "system_prompt"`).
- Renames: the first run read a moved file as a new file and asked about its old contents again.
  The diff is read with rename detection now, so a pure rename asks nothing and a rename with
  edits asks about the edits.

## Cost of a commit

Run over the last 60 non-merge commits of `origin/main` (median 3 Python files, largest 381), on
a loaded 10-core Mac (load average about 38): median 0.18 s per commit, 0.47 s at most for the 43
commits touching 15 files or fewer, 1.7 s for the 381-file commit; 24 of the 60 commits raised a
site, 10 of the 43 small ones. `hints.py branch` over a 34-file branch took 0.41 s at load average
97. Cost follows the diff: one `git diff`, one `git cat-file --batch` for the changed files, and one
parse and one walk of each changed Python file.

Parent: [[scripts/lint/docs/diff-hints/diff-hints.meiva.okf.md|diff-hints]].
