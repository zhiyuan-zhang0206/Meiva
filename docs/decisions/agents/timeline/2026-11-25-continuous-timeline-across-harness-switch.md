# One agent's timeline stays continuous across a harness switch; its context does not

Supplements decision 8 of
[an external agent's timeline is read from the CLI's record](../graph/2026-11-13-timeline-read-from-the-record.md)
(harness switches stop being shown) and builds on
[a harness switch keeps the session records](../graph/2026-11-12-harness-switch-keeps-session-records.md).
Decisions 1 to 7 of the first stand: the record is the one source of content, an item is named by the
row it comes from, and nothing derived is stored.

## Context

An agent can change harness on restart (`meiva`, `claude_code`, `codex`, `antigravity`). Each harness keeps the
conversation somewhere else: the built-in one in the database checkpoint, each CLI in its own record file on the
agent's machine. The timeline page read only the source of the harness the agent is on now, so after a switch the
person saw the agent's history end at the switch: the earlier harness's conversation was still there (the session
records are kept), but no page could reach it. Decision 8 of the 2026-11-13 record hid the switch itself as well,
because what Meiva wrote into the timeline for it was not part of any record.

The project owner ruled on 2026-11-25:

1. Continuity means the same agent's timeline across its own switches. It is not a fork lineage view and not a
   merged view of several agents.
2. Decision 8 stands in one respect: a switch is not a divider and not a reply. It may be one block, beside
   thinking and tool calls inside the work block.
3. A small table of facts is accepted. A copy of content is not.
4. Nothing carries the conversation over to the new harness, by any mechanism.

## Decision

1. **The unit is the harness epoch.** `agent_harness_epochs` holds one row per stretch of an agent's life on one
   harness: `(agent_id, seq)`, the harness, the model, the CLI session id once bound, `started_at` and `ended_at`
   (NULL for the current epoch), optionally the byte range of the record that belongs to the epoch, who switched
   (`birth`, `user`, `self`, `operator`, `backfill`) and the restart message, truncated. It stores facts about when
   and where, never what was said. A new agent gets epoch 0 in its birth transaction. Only a change of harness opens
   an epoch; a restart that keeps the harness does not.
2. **The switch is one transaction.** Closing the current epoch, opening the next and updating
   `agents_meta.harness` commit together, on the operator route and on the host's application of a self-restart
   alike. The column keeps meaning "the harness now"; `agents_meta.harness` is no longer immutable and the schema
   comment that said so is corrected.
3. **The coordinate of an external epoch is a byte range.** A CLI session file is shared by every epoch an agent
   spends on that CLI: after meiva, then codex, then meiva again, the two meiva epochs read different byte ranges
   of one file, and a time slice alone would repeat the first. The runner on the agent's machine reads the file's
   size at the switch (`prepare-harness-switch`) and the epoch keeps it. The built-in epoch is cut by time instead,
   because `state.messages` is rewritten by a compaction: the epoch's `started_at` and `ended_at` select the
   messages by `created_at`, the system prompt belonging to the first built-in epoch. An epoch without a stored
   coordinate (one rebuilt by the backfill) is read by time as well.
4. **The timeline is composed when asked for, from the sources, and stored nowhere.**
   `GET /api/agents/{id}/timeline` keeps its shape. The current epoch's items are exactly what they were: same ids,
   so the live stream, the sink and the streaming overlay change nothing. A `before=` cursor that reaches the head
   of the current epoch continues into the previous one, and so on back; the ids of an earlier epoch's items carry
   the prefix `e<seq>.` (`e0.h123.0`, `e0.12.0`), and the cursor grammar and `item_sort_key` accept it. A page
   reads one source, with the byte budget and timeout it had. The total order is `(epoch seq, order within the
   epoch)`, so the page no longer compares `created_at` across sources. `has_more` is true while any earlier
   epoch or earlier item exists. `msg_count` stays the current epoch's count.
5. **The switch is shown as one work-level item.** The start of every epoch after the first carries a synthesized
   item, `kind="system_marker"`, `source="harness_switch"`, id `e<seq>.sw`, whose English payload names the harnesses,
   the new model and, when there is one, the restart message summary. The page renders it in the work block, at
   the level of a thinking or tool item: not a divider, not a reply. This is the whole of what is lifted from
   decision 8; restarts that keep the harness, lost turns, unconfirmed inputs, fork markers and the seed stay hidden.
6. **A source that cannot be read is skipped.** The record on an offline machine, a CLI record that was cleaned
   up, a record left behind by a transfer: the earlier epoch contributes no history and no placeholder (its switch block still
   stands where the epoch begins), the page serves everything else, and the gateway logs a warning event naming the agent and the epoch. For an earlier
   epoch this replaces the 503 of the 2026-11-13 consequences "a machine that is offline shows no history": the
   segment is missing from the view, the page is not refused.
7. **Not supported for earlier epochs, for now.** Items of an earlier epoch have no `reading_locator` and are not
   reachable by `/timeline/window`. Sub-agents of a CLI stay one tool call in the main record.
8. **A harness switch shares no conversation, and the restart message is the handoff.** The new harness starts
   with its own history and its own system prompt. Meiva does not replay the earlier conversation, does not
   summarize it, and does not append a note about it to the built-in checkpoint when the built-in harness comes
   back. The agent hands over by what it writes in the restart message (`meiva.self.restart(message, ...)`,
   `meiva.agents.restart`, `meiva agents restart -m`, `POST /api/agents/{id}/restart`): the
   state of the work, and, when that is long, the path of a file that holds it. The message arrives as the first
   note on the new harness. The skill `meiva-guide/agents`, the SDK and CLI help and the switch
   note all say this in one voice. The web UI offers no switch: its Restart keeps the harness, with a fixed
   message and no config, so a switch is started only by the agent itself (`meiva.self.restart`) or by an
   operator through the SDK, CLI or REST API.
9. **One switch note.** The note the new harness receives (`[Harness switched from 'a' to 'b' by <who>]`, the reminder that
   nothing said before the switch is visible, then the restart message) comes from one function; the three copies of the format
   in the lifecycle text, the operator route and the runner are removed with the dead code around them
   (`claimed_harness_switch_target` and the unreachable protection in the operator route). The note that
   leaving the built-in harness wrote into the native checkpoint is removed too: the only place a switch is
   shown is the epoch table, through the switch block, so the built-in epoch cannot show it twice.
10. **Backfill is best effort.** For agents that switched before the table existed, a data migration rebuilds
    epochs from the `[Harness switched from 'a' to 'b']` system notes of the inbox. An agent whose notes do not
    form one consistent chain ending at its current harness (or has none) gets a single epoch on the current
    harness with `switched_by='backfill'` and no coordinates. Session records live outside the database and
    `harness_configured_at` is overwritten on each switch, so the migration uses neither. It is idempotent and
    assumes no old note exists.

## Alternatives rejected

- **Replay or summarize the earlier conversation into the new harness.** A replay is a second copy of the history
  that the new harness may read differently than the one that produced it, and a summary is a judgment made by
  nobody who owns it. The agent is the one who knows what the next run needs, and it can write that down; so the
  switch hands over exactly the message the caller wrote. This is the same stance as a compaction: the agent
  carries forward what it chose to.
- **Append a switch note to the built-in checkpoint when it resumes.** The checkpoint is the built-in harness's own
  history; a note about a conversation it never saw gives the model a reference to something it cannot read.
- **Merge the sources by `created_at`.** The CLI's record has its own clock, the checkpoint is rewritten by
  compaction, and a session file shared by two epochs repeats rows. An order of `(epoch, position in the source)`
  needs no clock agreement.
- **Store the CLI's rows to keep history when a machine is offline.** Still the rejected layer of the 2026-11-13
  record; per-segment unavailability is the cheaper answer for what is unreachable.
- **A divider at every switch.** Contradicts decision 8 and the owner's ruling: a switch is something the agent
  did, at the level of a tool call, not a break in the conversation.
- **A fork-lineage view or a merged view of several agents.** Another question; the unit here is one agent.

## Data migration

The table is created and the epochs of existing agents are backfilled by a migration, as best-effort and idempotent
as decision 10 says. It does not assume the old switch notes exist. The migration's own header lists what it
rebuilt from and what it merged.

## Consequences

- A person scrolling up past a switch reaches the earlier harness's conversation, with a switch block in between,
  without any change to the live path.
- The agent on the new harness cannot read, search or quote what it said before the switch. An agent that did not
  write a handoff starts cold; what the earlier harness did to the repositories and files is still there.
- Switching back to a harness resumes its own session record (decision of 2026-11-12), so that harness continues
  its own conversation, and sees none of what happened elsewhere in between.
- A second source is read only when the page crosses an epoch boundary, so the cost of a page is unchanged for
  anyone who stays within the current epoch.
- Earlier epochs' items have no reading locator, so a bookmark cannot point into them yet.
