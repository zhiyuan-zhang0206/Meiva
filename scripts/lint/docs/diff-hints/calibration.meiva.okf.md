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
head the review judged). That is 2,109 changed files.

| Measure | Value |
|---|---|
| Sites raised | 745 (0.35 per changed file) |
| Answers needed at path level | 482, of which 247 are `deleted-test` (one per file; one per directory with a directory trailer) |
| Reviewed findings | 73 |
| Findings that are a diff shape | 38 (52%) |
| Of those, a hint fired on the cited site | 33 (87%); all findings: 45% |
| Sites that are a confirmed misfire | about 7% (51 of 745: 7 of 30 sampled `ambient-read` sites, 5 of 43 `test-seam`, 3 of 45 `lambda-dependency`, one or two each in four small ids) |

Per id, over the five pull requests (sites, files, reviewed findings hit):

| Id | Sites | Files | Findings hit |
|---|---|---|---|
| `ambient-read` | 164 | 70 | 3 |
| `deleted-test` | 247 | 247 | 6 |
| `service-locator` | 135 | 35 | 1 |
| `lambda-dependency` | 45 | 22 | 2 |
| `test-seam` | 43 | 27 | 3 |
| `mutable-in-frozen` | 34 | 24 | 2 |
| `wildcard-case` | 10 | 9 | 2 |
| `protocol-outside-base` | 27 | 15 | 4 |
| `test-subclass` | 11 | 11 | 6 |
| `any-collaborator` | 10 | 6 | 2 |
| `none-fallback` | 8 | 6 | 1 |
| `callable-default` | 6 | 5 | 0 |
| `callable-bag` | 3 | 3 | 1 |
| `spy-assert` | 2 | 2 | 0 |

What the calibration changed, each from a sample of the hits:

- `any-collaborator` first matched every `value: object` of a parser (most of its 53 hits). It now
  needs a collaborator name; `Any` and `object` are both judged by it.
- `none-fallback` and `mutable-in-frozen` first fired in test builders and recorders (frozen
  `body: dict` payloads, `clock or SystemClock()` defaults); they judge production files only.
- `test-seam` first listed 63 `time.sleep` hits, nearly all child scripts and poll loops that
  are the recommended real-resource form; it now judges `test_*.py` only and not a sleep that
  paces a loop. `del sys.modules[..]` cleanup is not a seam.
- `test-subclass` first included every third-party base (`BaseChatModel`, `httpx.BaseTransport`:
  implementing an abstract interface); only database and cache clients remain, which is the
  `ParkedPool(AsyncConnectionPool)` case.
- A field compared with a literal was added to `wildcard-case` and removed again: 4 of its 17
  sites were on a reviewed finding, the rest compared wire kinds (`item.kind == "system_prompt"`).
- `ambient-read` stays broad. About 40% of its sites are a `time.monotonic` deadline in a poll
  loop; the answer is one line (`shell loop; the decision is unmet_step`) per file or directory.

## Cost of a commit

Run over the last 60 non-merge commits of `origin/main` (median 2 Python files, largest 381):
median 0.16 s per commit, 0.28 s at most for the 49 commits touching 15 files or fewer, 2.7 s
for the 381-file commit; 20 of the 60 commits raised a site, 10 of the 49 small ones.
Cost follows the diff: `git diff`, one `git cat-file --batch` for the changed files, and one
parse and one walk of each changed Python file.

Parent: [[scripts/lint/docs/diff-hints/diff-hints.meiva.okf.md|diff-hints]].
