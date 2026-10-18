---
type: doc
title: Memory Recall Regression Set — Two Directions
description: A 55-case passive-recall regression set, 30 turns where a usable note was never delivered and 25 where nothing applied yet the ranking fired, with the 2026-10-18 baseline, the held/slipped/lost and fired/silent evaluator, and the resolution limits of the set.
---

# Memory Recall Regression Set

`services/derived/memory_indexer/tests/recall_cases.jsonl` holds 55 real inbound turns in two
halves, because passive recall fails in two directions and a set that only measures one of them
cannot judge a threshold:

- **recall set — 30 cases.** A note the agent needed *was retrieved* and scored under the
  injection threshold, so the turn injected nothing. 24 sat at rank 1, 6 at rank 2, scores
  0.65–0.79; 20 of them are delivered by the shipped 0.72 default, all 30 were silenced by 0.80.
- **precision set — 25 cases.** Nothing in the pool applied to the turn, yet the ranking was
  confident. 15 fire at the shipped default (top scores 0.72–0.84), 5 sit just under it
  (0.63–0.70) and 5 stay silent (0.36–0.50). These are receipts, machine readouts and small talk —
  the traffic that produces a plausible-looking but useless note. The five in the middle exist so
  that the *next* step down, 0.72 → 0.65, moves this half as well: without them it would report
  "recall up, precision unchanged" purely because no case scored between 0.50 and 0.72.

`recall_regression.py` scores a fresh run into **delivered / held / slipped / lost** for the recall
half and **fired / silent** (plus *newly firing*) for the precision half; a precision case fires if
*any* of the top `cap` matches clears the threshold, not only the first. The threshold and the cap
are read from the agent config (`memory_recall_inject_threshold`, `memory_recall_inject_max`), never
copied into this module, so a re-calibration cannot leave the report judging at a stale cut.

## Running it

A results file maps a case id to the ranked matches a fresh run returned, best first:

```json
{"recall-01": [{"path": "meiva/ci/monitors/ci-watch-discipline.md", "score": 0.74}, {"path": "...", "score": 0.61}]}
```

Produce it with whatever runs the ranking — the same request passive recall makes, so `purpose`
must be `recall` and `k` at least the 30 candidates the ranking considers. Inside an agent or any
process holding a `MeivaContext`:

```python
from base.agents.context.gateway_client import transport

for case in load_cases():
    body = transport.post(
        "/api/memory/search",
        json={"query": case.message[-2000:], "k": 30, "purpose": "recall"},
        timeout=180,
        context=ctx,
    ).json()
    results[case.id] = [{"path": m["path"], "score": m["score"]} for m in body["results"]]
```

Then:

```bash
.venv/bin/python -m services.derived.memory_indexer.recall_regression --results /tmp/recall.json
```

`--threshold` / `--cap` override the config, for judging a run taken under a different setting.

## Reading the pair

The baseline on 2026-10-18 at 0.72 was **delivered 20/30, fired 15/25**. Judge a change by both
numbers at once:

- **Lowering the threshold** should raise *delivered* and raise *fired* too. That is expected and
  is not evidence for or against the change by itself; the sizes are what matter.
- **Raising it** should do the reverse. If only one half moves, the change is not doing what it
  claims.
- **A new question, model or chunker** should move neither at first; if it moves either, check the
  cases by hand before believing the average.
- **A case that slipped, was lost, or newly fires** is named in the report. Read it before
  explaining it away.

Re-record `baseline_rank` / `baseline_score` / `baseline_delivered` only when the retrieval stack
itself changed, never when the cut point moved, and say which stack produced the new numbers.

### How fine a cut this set can resolve

The two halves are not equally sharp. At 0.72 the next step down (0.65) moves recall **20 → 30**
and precision **15 → 19**, so that step is judged by the set itself rather than by arithmetic; but
recall saturates at 30/30 for anything at or below 0.65 and precision only creeps (19 at 0.65,
20 at 0.63, 21 at 0.50), so **cuts below 0.65 are not distinguishable here** — they need new cases,
not a re-reading of these. The set also records one note per turn: it says whether a turn was
silenced, never how many extra notes a looser cut would have stacked on top.

## When to run it

Run it and paste the report into the PR whenever a change touches retrieval or the recall gate —
the ranking question, `memory_recall_model`, the candidate count, chunking, `memory_recall_inject_*`
— and say in the PR whether the two halves moved and how much. Nothing in CI runs it: it needs a
live gateway, an index and a model, so it is a tool a reviewer reads, not a gate.

## Data

The cases are real internal work traffic from the cluster (one week, 48 agents), kept because a
synthetic paraphrase would not reproduce the retrieval. They carry no absolute paths, no
addresses, no tokens and no agent identifiers; messages that had a machine-local path were left
out. They live in this private repository on purpose. CJK is stored as JSON `\uXXXX` escapes
because the repository allows no raw CJK, and each case carries a one-line English `gloss` so the
set is reviewable without decoding it. Read one back with `json.loads(line)["message"]`.
