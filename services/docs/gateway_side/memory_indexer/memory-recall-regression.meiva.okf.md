---
type: doc
title: Memory Recall Regression Set — 30 Blocked-Answer Turns
description: What the passive-recall regression fixture holds, how a fresh run is scored against the 2026-10-18 baseline, and why the cases are the ones the 0.80 threshold used to silence.
---

# Memory Recall Regression Set

`services/derived/memory_indexer/tests/recall_cases.jsonl` holds 30 real inbound turns on which
the note the agent needed **was retrieved** but scored below the injection threshold, so the turn
injected nothing. Each case records the message, the note that should have surfaced, and the rank
and score it held on 2026-10-18. `recall_regression.py` turns a fresh run into a held / slipped /
lost report.

The cases are the failure mode, not a random sample: 24 sat at rank 1, 6 at rank 2, with scores
0.65–0.79. They came out of replaying 523 real inbound messages (7 days, 48 agents) through
`purpose=recall` with 30 candidates; the full measurement, its two judge passes and its
limitations are in the decision record that lowered the threshold
(PR #6239, which also lowered the threshold to 0.72).
Messages containing a machine-local absolute path were left out so the fixture stays portable;
messages appear as they arrived, capped at the 2,000 characters a query is capped at. They are
stored with JSON `\uXXXX` escapes because the repository allows no raw CJK, and each case carries
a one-line English `gloss` so a reviewer can tell what the turn is without decoding it. Read one
back with `json.loads(line)["message"]`.

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

The report counts **delivered** (top three and at or above the current threshold), **held** (same
rank or better), **slipped** (still returned, ranked lower) and **lost** (no longer returned), and
names every case that moved with both ranks and both scores. `--threshold` takes the threshold in
force when the run happened.

## What a change should look like

- Nothing moved: the ranking change is neutral for this set. Say so in the PR; the set is small
  and its 30 cases are the boundary band, so silence here is weak evidence, not proof.
- A case **slipped** or **lost**: the note an agent needed moved further from the top. That is a
  regression even if the average score rose — check the case by hand before explaining it away.
- The threshold moved: `delivered` changes by construction. Re-record `baseline_rank` /
  `baseline_score` only when the retrieval stack itself changed, not when the cut point did.

Re-recording is `--results` on a known-good stack, copying the observed rank and score back into
the fixture. Do that in the same PR that changes retrieval, and say which stack produced it.
