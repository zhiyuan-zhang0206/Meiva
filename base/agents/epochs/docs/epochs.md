# Harness epochs

## What it is

`agent_harness_epochs` records which harness an agent ran on and when: one row per stretch of time on one harness, numbered `seq` 0, 1, 2 per agent. It stores facts only, never conversation content. A harness keeps its own conversation (the built-in `meiva` harness in its checkpoint, an external CLI in its record file), and a timeline reader joins these rows to those sources.

A harness switch shares no conversation: the new harness cannot see anything said or done before the switch. The restart message is the only handoff: put the handoff in it, and if it is long, write it to a file and point to that path in the message.

## Columns

- `harness`, `model`: what the stretch ran on. `model` is NULL for backfilled rows other than the current one.
- `session_id`: the external CLI session of the stretch. It is stored when a switch leaves the harness (or arrives in a harness that resumes a session), so a stretch that has been left always names its session; the current stretch is read through the session registry.
- `started_at`, `ended_at`: the stretch in time. `ended_at IS NULL` is the current stretch, and a unique partial index allows at most one per agent.
- `source_start`, `source_end`: byte offsets into the session's record file. They exist because one CLI record can serve two stretches of one harness: for A, then B, then A, the file of A does not grow while B runs, so stretches 0 and 2 read different byte ranges of the same file. NULL means unknown (a backfilled row, a built-in stretch, a packed record), and the reader falls back to `started_at` and `ended_at`.
- `switched_by`: `birth`, `user`, `self`, `operator`, or `backfill`.
- `reason`: an excerpt of the restart message that asked for the switch.

## Writes

Only a change of harness opens a stretch; a restart that keeps the harness writes nothing here.

- Birth: `base/agents/birth/transaction.py` writes `seq` 0 in the birth transaction.
- Switch: `base/agents/epochs/write.py:harness_switch` returns the statements that close the current stretch, open the next and store `agents_meta.harness`. Both paths run them inside the transaction that holds the agent row: the operator path (`ops/lifecycle/restart_row.py`) and the agent's own deferred restart (`agent/ownership/lifecycle_plan.py`, applied by `agent/ownership/hosted.py`).
- Offsets: `base/agents/epochs/extent.py:record_extent` reads the record file length on the machine that holds it. The leaving harness is read after its session has stopped; the arriving one is read when it resumes an existing session, so its stretch begins where the file ends now.
- Backfill: migration `20261125T062100_agent-harness-epochs-backfill.sql` rebuilds the stretches of existing agents from the `[Harness switched from 'a' to 'b']` system notes in `inbound_messages`. An agent whose notes do not form one consistent chain ending at its current harness gets a single stretch on the current harness, `switched_by = 'backfill'`. Session records and `harness_configured_at` live outside the database or are overwritten, so the migration does not use them.

## Reason excerpt

`reason` keeps the first 500 characters of the restart message. The whole message stays in the inbox row it arrived in; the epoch row only needs a line that says why a switch happened, and a status table should not become a second message store. The bound is a self-imposed storage guard, not a limit anyone tunes.

## Reading

`base/agents/epochs/read.py:read_epochs` returns the rows in order. An agent with no row (a fixture that wrote `agents_meta` directly) reads as empty; callers treat the harness on the agent row as its one stretch.

## Entry points

- `base/agents/epochs/write.py:harness_switch`
- `base/agents/epochs/write.py:birth_epoch`
- `base/agents/epochs/extent.py:record_extent`
- `base/agents/epochs/read.py:read_epochs`
