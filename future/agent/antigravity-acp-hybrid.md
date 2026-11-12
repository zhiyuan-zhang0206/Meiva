# Antigravity: move to ACP, keep a private RPC for mid-turn injection

Plan to replace the print-mode transport of the Antigravity (`agy`) adapter with
`agy acp` as the main path, and to keep one non-public local interface only for
injecting a message into a running turn. Current behavior is described in the
[Antigravity bridge](../../base/lm/harness/antigravity/docs/antigravity.md); this
document owns what changes and why. Facts below were probed on agy 1.5.6 on
2026-11-12 with short turns on a real CLI.

## Status

Not started. The migration begins after the message-semantics questions in
[Open questions](#open-questions-that-gate-the-migration) are decided, because
they fix what the adapter has to deliver.

## Current state and what hurts

The adapter drives `agy --input-format stream-json --output-format stream-json`
(print mode). That protocol has no request ids, which costs:

- **Interrupt kills the process.** A cancel cannot be a request, so the adapter
  sends `SIGINT`; the CLI exits and takes its scheduled tasks with it. The next
  delivery respawns it with `--conversation`.
- **Images arrive late.** stdin has no image field. An image for an idle
  conversation is sent as a second input through a non-public RPC after the turn
  has started, and is dropped if the turn does not run within the timeout.
- **A denied tool still reports success.** Headless mode auto-denies a tool the
  policy does not allow, yet the turn ends `status: SUCCESS`; the adapter
  reconstructs the failure from `denied_actions`.
- **Four non-public interfaces** carry real behavior: the `SendUserCascadeMessage`
  and `GetCascadeTrajectory` RPCs, the message-queue files used for
  reconciliation, and the sub-agent descriptor and transcript files.

## Measured facts

Pure ACP (`agy acp` over stdio, 15 short turns):

| Topic | Observed |
|---|---|
| Cancel | `session/cancel` ends the turn with `stopReason: cancelled`; the in-flight command is killed, the process and session survive, and the next `session/prompt` works immediately |
| Second prompt mid-turn | A second `session/prompt` preempts: about 10 ms after it arrives the first returns `stopReason: end_turn` (not `cancelled`) with its answer unfinished; only the second prompt is answered. It is not an injection |
| Sub-agents | `invoke_subagent` completes at once and no update frame carries the sub-agent's text or tool calls |
| Delivery echo | `user_message_chunk` echoes the text but carries none of our ids, so it is matched by text and order |
| Permissions | Default mode raises `session/request_permission`; a rejection makes the tool call `failed` |
| Images | Sent as content blocks in `session/prompt` and recognized |
| Usage | Returned with every `session/prompt` result, cancelled turns included |
| Resume | `session/load` replays history, `session/resume` does not; one process serves several sessions |
| Thinking | At the default thinking level only a `thoughtTokens` count appears; no thought text |
| Compaction | No notification; only a `/compact` slash command |
| Unsolicited turns | With an 8 s timer set, 40 s passed with no unrequested frame and the timer did not fire. This does not show that phantom turns are gone |

Hybrid (ACP plus the local Connect RPC, about 10 short turns):

| Topic | Observed |
|---|---|
| Local interface | The `agy acp` process exposes the same local Connect RPC, accepting the token we set in the environment; the ACP `sessionId` equals the RPC `cascadeId`. The existing port discovery and token handling carry over |
| Mid-turn injection | `SendUserCascadeMessage` during a running turn does not interrupt it; the turn ends `end_turn` and the model answers the injection in the same turn. The echo appears only when the model reads the message: injected at 6.8 s, echoed at 13.3 s |
| Injection while idle | Still a phantom turn. ACP shows no frame for about 25 s; the next `session/prompt` replays it and its usage roughly doubles (10504 to 22404 input tokens). Idle input must go through `session/prompt` |
| Cancel after injection | Cancel is still clean. The unread injected message is delivered before the next prompt's text and does not start a turn of its own; the following answer addressed only the later prompt |
| Sub-agents | Same files as print mode; the RPC can also query a child session's steps. A sub-agent can still be running when the parent turn ends |
| Thought text | At a high thinking level ACP emits `agent_thought_chunk` frames |

Excerpts (session ids and long fields trimmed):

```text
# mid-turn prompt preempts (pure ACP)
 8.92 -> session/prompt   "Also reply with the word SECOND."
 8.93 <- {"result":{"stopReason":"end_turn",...},"id":4}     # first prompt, unfinished
15.47 <- {"result":{"stopReason":"end_turn",...},"id":5}     # only SECOND answered

# cancel (pure ACP)
10.32 -> session/cancel
10.33 <- {"result":{"stopReason":"cancelled","usage":{...}},"id":4}

# injection during a turn (hybrid)
 6.82 HTTP-> SendUserCascadeMessage {"cascadeId":"<sessionId>","items":[{"text":"...BRAVO..."}],"clientMessageId":"hy-inject-2"}
 6.83 HTTP<- 200 {}
13.34 <- session/update user_message_chunk "...BRAVO..."
15.79 <- session/update agent_message_chunk "ALPHA BRAVO"
15.82 <- {"result":{"stopReason":"end_turn",...},"id":4}

# injection while idle (hybrid): phantom turn
 5.46 HTTP-> SendUserCascadeMessage {...,"clientMessageId":"hy-idle-1"}   # no ACP frame for ~25 s
30.09 -> session/prompt   "Reply with the single word TWO."
30.09 <- user_message_chunk "...PHANTOM."  /  agent_message_chunk "PHANTOM"
30.11 <- user_message_chunk "...TWO."
```

## Plan

Division of labor:

| Job | Owner after the move |
|---|---|
| Open a turn, stream output, tool calls, thought text | ACP `session/prompt` and `session/update` |
| Cancel | ACP `session/cancel`; the process stays up |
| Permissions | ACP `session/request_permission` |
| Images | Content blocks in the same `session/prompt` |
| Usage | The `session/prompt` result |
| Mid-turn injection and its in-context confirmation | The local RPC only: `SendUserCascadeMessage` out, `user_message_chunk` echo in |
| Sub-agent content | The descriptor and transcript files, as today |

Removed with the move: killing and respawning the process on interrupt, the
second-input image follow-up and its timeout, the denied-action reconstruction of
a failed tool, usage estimation, and the stdin/stdout framing.

Kept: port and token discovery, the version premise with fail-fast on an unknown
reply shape, the sub-agent file reader, the reconciliation of queued messages,
and the observation of phantom turns.

Invariants the new transport must hold:

1. **One in-flight `session/prompt` per session.** A second prompt preempts the
   first and reports `end_turn`, so a message for a running turn is never sent as
   a prompt.
2. **Never inject through the RPC while idle.** Idle input always starts a turn
   with `session/prompt`. The race where the conversation is running when asked
   and idle when written to remains and keeps its phantom-turn observer.
3. **An injection echo is not new user input.** The `user_message_chunk` for an
   injected message, including the residual one that precedes the next prompt
   after a cancel, is matched to the injection by text and order and recorded as
   its acknowledgement, never as a fresh inbound message.

## Not yet verified

- Injection of a message that carries an image.
- Injection under `--process-per-session`.
- Whether the RPC is still usable after `session/load` or `session/resume`
  reconnects.
- Whether pure ACP produces spontaneous turns (timers, completed background
  work) over a longer window than the 40 s tried.
- What a cancel does to a running sub-agent and to background (`nohup`)
  processes.

## Open questions that gate the migration

The migration starts only after these are decided, since they set the delivery
shape this adapter must implement for all external harnesses:

1. **Input form for several messages.** One input with one content block per
   message and each image next to its own message, instead of one joined text
   with all images last?
2. **Sub-agent output after the parent turn ends.** Show it attached to the call
   that started it without opening a turn of its own, for every harness?
3. **Turns the CLI opens itself.** Mark their cause, or leave them unmarked and
   drop the cause classification?
4. **Feedback after Stop.** Show the interruption immediately instead of with the
   next input?
5. **Cluster sub-agent completion.** Notify the parent agent automatically, as a
   finished background shell does?
