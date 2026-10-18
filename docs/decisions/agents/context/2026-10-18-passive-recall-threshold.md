# Passive recall injects at 0.72, not 0.80

## Context

`memory_recall_inject_threshold` decides whether a retrieved note reaches the
agent at all: the gateway scores every candidate (`jev-1.13.0`, the `recall`
question in `memory_indexer/rerank.py`), and passive recall injects the notes at
or above the threshold, best first, at most three. The 0.80 default came from
the 2026-10-11 benchmark.

That benchmark asked whether a *relevant* note ranks first; production asks a
different question — how often the agent ends up with anything. On 2026-10-18 we
replayed 523 real inbound messages (7 days, 48 agents, deduped; the messages
agents actually receive, 91% of them peer work traffic) through the production
recall path: same question, same 30 candidates, same threshold.

What came back:

| Outcome (523 turns) | share |
|---|---|
| injected, and the note was usable | 57.2% |
| injected, note was noise | 0.8% |
| **silent, though a usable note sat in the top five** | **35.0%** |
| silent, note existed but was not retrieved | 0.8% |
| silent, no usable note in the pool | 6.3% |

Of the 183 silent-but-answered turns, 123 had that note at **rank 1**, and the
scores clustered at a median of 0.72 (p90 0.78) — below the threshold, above
nothing. Ranking was not the problem (1 of 523 had its best note beyond rank 5),
retrieval was not the problem (4 of 523), and the language/type buckets (zh/en;
prose/paths/identifiers) differed by at most three points.

Retrieval quality itself was fine: a usable note in the top five on 92% of turns
(83% under a stricter, quote-anchored judge) and at rank 1 on 72% (56%).

## Decision

Ship `memory_recall_inject_threshold = 0.72` as the default.

Measured trade-off on the same 523 turns (lenient / strict judge):

| threshold | turns injected | recall of answered turns | turn precision | false injection |
|---|---|---|---|---|
| 0.80 | 57.9% / 52.5% | 59.3% / 55.0% | 94.4% / 87.3% | 3.3% / 6.7% |
| **0.72** | **81.3% / 79.2%** | **80.7% / 79.0%** | **91.5% / 83.2%** | **6.9% / 13.3%** |
| 0.70 | 84.9% / 82.5% | 83.6% / 81.0% | 90.8% / 81.8% | 7.8% / 15.0% |

## Alternatives rejected

- **Keep 0.80.** It is precise (1% noise) and leaves a third of answered turns
  silent, with the answer already at rank 1. The cost being avoided is a few
  hundred context tokens; the cost being paid is a rule the agent never sees.
- **0.70.** Three more points of recall for six more points of false injection
  per judge; the curve flattens after 0.72.
- **0.75.** The conservative middle: it recovers two thirds of the gap
  (recall 71%) at half the precision cost. Rejected because the marginal notes
  between 0.72 and 0.75 are mostly usable and the cap of three bounds the noise.
- **A rank-shaped rule** ("inject top-1 at ≥0.65 when nothing reaches 0.80").
  Measured worse than a flat 0.72: false injection 15.9% / 25.8% of turns,
  because when nothing scores well the top note is usually the wrong one.
- **Hybrid lexical retrieval.** A BM25 channel recovers 4 of 523 turns (0.8%);
  the misses are real (title-equals-identifier cases) but too rare to justify a
  second retrieval path today.
- **Rewriting the query before retrieval.** Recovers ~4% of turns (21 of 65 hard
  cases under strict judging), concentrated on machine-formatted messages, and
  the notes it finds score 0.6–0.8 — so it still needs this threshold change to
  have any effect. A later, separate change.
- **Re-wording the rerank question.** That is a different lever (precision of
  the score, not the cut point) and belongs with the memory dedup work.

## Consequences

- Each turn injects about 0.4 more notes, roughly 300 more context tokens.
- Turn precision drops 94% → 92% (lenient judge) or 87% → 83% (strict); false
  injection rises 3.3% → 6.9% / 6.7% → 13.3%. In exchange, recall of answered
  turns rises 59% → 81% (55% → 79%).
- The 2026-10-11 benchmark no longer calibrates this field. It still calibrates
  `memory_dedup_candidates`, and `memory_write_dedup_threshold` keeps its own
  0.80 — the two thresholds answer different questions and now move separately.
- **Scope: the default only.** The setting is `cluster-pinned` and per-agent, so
  this changes the value an agent gets when nothing else pins it. Any host with
  `MEIVA_MEMORY_RECALL_INJECT_THRESHOLD` in its env, and any agent with a stored
  override, keeps 0.80 until that pin is changed. Rollout is a separate operator
  action, and the same 523-message replay should be run afterwards to confirm the
  false-injection rate stayed inside the measured band.

### How the numbers were produced

523 messages, one search each through `/api/memory/search` with
`purpose=recall`, `k=30`; two independent judge passes (lenient: a judge with a
written rubric; strict: the same judge required to quote the sentence the agent
would apply), a shuffled-pair control (3.7% of unrelated pairs called usable),
and 20 hand-labelled pairs (17 agreed). Provenance: **2026-10-18, 21:50–22:20 +08**, machine `ubuntu`, agent 6850,
against cluster `main` at `ffaf8ea87` (the replay used the deployed gateway, so
its index is the one that commit's release built). Workspace artefacts:
`~/.meiva/workspaces/6850/sprint-report.md` (the full report),
`eval12-data/sprint_search_all.json` (per-turn top-30 with scores),
`eval12-data/sprint_judge_sonnet.json` and `sprint_judge_strict120.json` (the two
judge passes), `scripts/repro_example.py` (one case, end to end).

The reproducible half now lives in the repository: the 30 turns this decision
silenced are the recall half of
`services/derived/memory_indexer/tests/recall_cases.jsonl`, and the evaluator
next to it scores a fresh run against their recorded ranks and scores (see
[[services/docs/gateway_side/memory_indexer/memory-recall-regression.meiva.okf.md]]).
