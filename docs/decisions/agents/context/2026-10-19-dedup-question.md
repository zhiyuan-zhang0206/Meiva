# The dedup question asks for one decision, not one wording

## Context

Two things ask `purpose=dedup` against the pool — the write-time hint an agent
gets after `meiva.files.write` (`memory_write_dedup_threshold`, default 0.80) and
the maintainer's weekly check — so both are only as good as the question the
ranker asks.

The shipped question defined a duplicate as a note that "records essentially the
same knowledge", and then excluded the case that matters: *"a note that adds a
different rule, a different scope, or a different decision about the same subject
is not the same knowledge."* Forks are the common failure, not the rare one: of
the same-rule nearest neighbours in the first sample below, 19 of 22 (86%) were
forks — one note carries a later exception the other does not. The exclusion
clause told the model to score exactly those low, and the cut point cannot
recover them: in that sample, same-topic-different-decision pairs sat at
0.51–0.57 while the fork of a real ruling sat at 0.22–0.44, so lowering the
threshold fires the wrong pairs first.

Measured on two independent 200-write samples:

| sample | with the shipped question at 0.80 |
|---|---|
| 200 random writes, 2026-10-17 | 5 reminders (2.5%), caught 5 of 26 mergeable duplicates (19%) |
| 200 random writes, 2026-10-18 (fresh period) | 1 reminder (0.5%), caught 1 of 21 (5%) |

How the samples were drawn, so the numbers can be recomputed rather than
trusted:

- **2026-10-17** — the 2,217 pool paths written since 2026-09-26, of which 1,886
  still existed; 200 drawn with `random.seed(20261017)`. Each note was scored
  through the production dedup request (k=5, itself excluded) and its best
  candidate kept. Labels came from a stratified read of 79 pairs (all of the
  ≥0.4 band, all of 0.2–0.4, 15 random each from 0.1–0.2 and below), which is
  where the 26 and the 86% fork share come from — a band-weighted extrapolation,
  not a count of the whole sample.
- **2026-10-18** — paths written 2026-09-01..09-25 (`git log` gave 2,026, of
  which 1,053 existed), 200 drawn with `random.seed(20261018)`; a second scoring
  pass re-read all 46 disputed pairs and kept 21 as mergeable duplicates. Its
  states were rebuilt from the same excerpt rules as production and reproduce the
  gateway scores to within 0.017 on average.

A live week of maintenance traffic agrees: 40 sampled writes produced no
candidate at 0.80 at all.

## Decision

Ask a different question — the same rule or decision, not the same wording:

> The existing note states the same rule or decision as the new note: an agent
> that read either one would take the same action, and the two are two records of
> one decision rather than two different decisions. Different wording, more or
> less detail, or a later exception added to one of them does not make the
> decisions different.

`true_if`: `The two notes record the same rule or decision.`
`false_if`: `They are two different decisions on the same subject, or share only a topic.`

Those two lines are part of the prompt
(`base/lm/catalog/decide/types.py::question_text` joins all three), so a
paraphrase here is a different prompt; a test pins the three strings.

The threshold stays at 0.80: the new question's curve peaks in precision there.

## Evidence

Same two samples, same states, same model (`jev-1.13.0`), re-scored with the new
question:

| sample | question | reminders /200 | recall | precision |
|---|---|---|---|---|
| 2026-10-17 | shipped | 5 (2.5%) | 19% | 100% |
| 2026-10-17 | **new** | 16 (8.0%) | **42%** | **69%** |
| 2026-10-18 | shipped | 1 (0.5%) | 5% | 100% |
| 2026-10-18 | **new** | 21 (10.5%) | **48%** | **48%** |

At 0.60 the 2026-10-17 sample gives 69% recall at 49% precision (37 reminders,
18.5% of writes with a reminder); at 0.85 and 0.90 the 2026-10-18 sample falls to
29% / 24% recall at 38% / 56% precision. 0.80 is the point on this curve where
half the reminders are real and half the duplicates are caught.

Label reliability: a second scoring model agreed with the hand labels on 76% of
the 46 disputed pairs, so read both columns as ±10 points. Every number above is
a count in the two sample files, not an extrapolation.

## What this changes for the reader

An agent that writes a shared note sees a one-line reminder after about one write
in ten instead of one in forty; of those reminders, roughly half name a note that
really does record the same rule. Nothing blocks, nothing is rewritten, and the
note it just wrote is still in context when the line arrives — the skim costs
seconds. The alternative is a channel that stays silent on the case it exists
for.

## Alternatives rejected

- **Lower the threshold on the shipped question.** The pairs that fire first when
  it drops are same-topic-different-decision ones (0.51–0.57), not forks
  (0.22–0.44): the ordering is wrong, not just the cut.
- **Leave the write hint alone and rely on the maintainer's weekly check.** It is
  the same question against the same index; the check would keep missing forks
  and pay a human to review the misses.
- **A separate "fork" purpose.** Two questions would age apart, and the write
  check has one slot.

## Scope

`dedup` only. The `recall` and `search` questions are untouched, so the 0.72
recall calibration recorded in
[[docs/decisions/agents/context/2026-10-18-passive-recall-threshold.md]] stands.
This is a wording change in `memory_indexer/rerank.py`, not a new threshold, and
`memory_write_dedup_threshold` keeps its 0.80 default.
