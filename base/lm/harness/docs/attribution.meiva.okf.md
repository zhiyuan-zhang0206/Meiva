---
type: doc
title: Who a provider message belongs to
description: 'The attribution split every harness adapter shares: `TurnFaulted`, `SessionFailed` and `UnsolicitedOutput`, the `_claim` entry point, the sink that delivers unowned output as inbound content, and the clean-queue rule that keeps an idle session from poisoning the next turn.'
tags:
- base
- library
- llm-inference
---

# Who a provider message belongs to

A provider sends more than the answers to what it was asked: it reports a
background command that finished after its turn ended, and — in the Claude CLI's
case — runs a turn of its own over that report. Every message therefore has to be
attributed before it becomes an event, and three events say what happened, each
carrying exactly what it knows.

| Event | Means | What a reader may do with it |
|---|---|---|
| `TurnFaulted(turn_id, error)` | this turn failed | end the turn it names; a turn id it does not name is not ours to fail |
| `SessionFailed(error)` | the session failed | end the turn that was waiting on it as a closed session, and rebuild; it never names a turn |
| `UnsolicitedOutput(source, text)` | the provider produced this on its own | deliver it as inbound content; never a turn boundary, never a failure |

`SessionFaulted` is the retired spelling of the first two: an unmigrated adapter
still constructs it with `error` and an optional `turn_id`, and it is the base of
both new events, so that adapter keeps working and a reader filtering for it keeps
seeing turn failures. What its `turn_id=None` *means* changed: nothing. The bridge
reads such a failure as the session's, never as the running turn's, so it no longer
reaches a turn.

## Three rules, none of them an adapter's job

- **`_claim(origin_id)`** is the one entry point: a message that names no turn is
  the running turn's, and a message that names one is the running turn's only
  when that turn carries the same name (`TurnTicket.provider_turn_id`, which an
  adapter sets once it learns its provider's name for a turn). An adapter passes
  its provider's own name and nothing else — it does not decide.
- **`_unsolicited(event)`** records the event and hands it to the session's sink,
  which the binding carries (`SessionBinding.unsolicited`). The sink belongs to
  the host, whose only writer of these reports is
  `base/agents/inbox/harness_notice.py`: one `system:harness` chat
  inbound, which the claim node turns into a turn like any other message. An
  adapter never writes an inbox.
- **Every turn starts from a clean queue.** `_start_turn` discards whatever nobody
  consumed while the session was idle — the stream's end marker excepted — so an
  event that belonged to no turn cannot be read as the next turn's outcome. That
  is what an idle session's failure used to do (task #5663).

## Holding dispatch

`_hold_dispatch(seconds)` parks turn dispatch — `submit` queues, `_active` stays
empty — while the provider runs a turn of its own, so that turn's text takes the
idle path instead of being collected into a turn of ours and committed as our
reply. `_release_dispatch()` ends it and starts the oldest queued turn; the bound
is the adapter's own knob, because only the adapter knows how long its provider
may take.

## What a report becomes

The host's writer is deliberately dull: one inbound row with source
`system:harness`, kind `chat`, and the report's text. `chat` and not `system_note`
because the point is to run a turn — a note nobody must answer would leave the
agent as unaware as before — and `system:harness` because the envelope then reads
as a framework note, which is what it is.
