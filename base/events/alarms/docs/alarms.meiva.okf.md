---
type: doc
title: Durable alarm records
description: Write the detection down before trying to send it, so a refused transport leaves evidence a later wake can read.
tags: [contracts]
---

# Durable alarm records

Every detector that notifies carries one unstated assumption: that notifying works.
On 2026-10-28 a burn-rate gate crossed its line, called `send_message`, got
`GatewayUnavailable: Connection refused`, and left one log line behind — nobody
learned until the next night. The detection was right; the delivery was the whole
of the evidence.

`base/events/alarms` makes the record the first effect and the send the second. It owns
one path, one format and one reader for every guard on a machine, so any periodic
job can answer "what fired while nobody was listening" without knowing which guard
fired it.

## Roles

| Call | Who makes it | What it means |
|---|---|---|
| `record()` | the detector, before it opens a transport | the detection happened, and it is now durable |
| `mark_delivered(error=...)` | the detector, after its send returns or raises | the transport's answer, success or the reason it refused |
| `open_alarms()` | any periodic job on its next wake | alarms nobody reported and nobody closed |
| `mark_notified()` | the reader that just reported them | this alarm has been put in front of its owner once |
| `acknowledge()` | whoever owns the alarm | this alarm is seen and being acted on |

`record()` appends one line and fsyncs before returning: a record that only
reached the page cache dies with the machine that was already in trouble. It opens
no socket and reads no configuration, so it works on a machine whose gateway,
database and network are all gone.

`mark_delivered(error=None)` records that the transport accepted the message, and
that is the end of the alarm for the reader's purposes: the sender did its job. A
refused send leaves the record open, and a later wake reports it to the alarm's
lead. The module never claims an accepted send reached someone — it claims only
what happened, so an operator reading the line can weigh it.

## Store

Records are JSON lines in `$MEIVA_HOME/state/alarms/<YYYY-MM-DD>.jsonl`, dated by
the day the detection happened on the writing machine — an operator reads
`ls state/alarms` as a calendar, and a day can be deleted without rewriting
anything. One event per line, keyed by a 12-character id:

- `raised` carries the whole alarm: `kind`, `subject`, `judgement`, `reading`,
  `owner`, `source`.
- `delivery` carries `error`, null when the transport accepted the message.
- `notified` and `acked` carry only their timestamp.

Free text is cut at 500 characters: the reading belongs in the record, the detail
belongs in the log the alarm points at.

## Reading

`open_alarms()` folds the dated files into one alarm per id, last event winning,
and returns those whose transport never took them (`delivered` false), that are not
notified, not acknowledged, and at least `older_than` old. Three properties come
from that shape:

- **The age guard belongs to the sender.** An alarm younger than `older_than` is
  still owned by the call that raised it, so a concurrent reader cannot report it
  out from under its own sender. The adopter passes a window longer than its
  slowest send.
- **The window is two days**, walked oldest first. An alarm raised late on one day
  is read by a wake on the next, and an event that lands after midnight still closes
  the alarm the previous day raised. Anything older is history the operator reads by
  hand, not a wake-up.
- **A delivered alarm is not re-reported.** The transport accepted it; echoing every
  successful send back to the lead would drown the ones that failed.

A line that does not parse (a process killed mid-append) is skipped, not fatal: it
must not hide the alarms around it. An event whose `raised` line fell out of the
read window is dropped rather than guessed at — the reader never invents an alarm
it cannot show in full.

`Alarm.line()` is the one-line form a report carries: when it fired, what it fired
on, the judgement and the reading behind it, the owner, and whether the transport
would take it.

## Adopting it

A guard adopts this module in three steps, without changing its own thresholds:
call `record()` before the send, call `mark_delivered()` with what the send
returned, and add `open_alarms(older_than=...)` to the report of the job that
already wakes on that machine.
