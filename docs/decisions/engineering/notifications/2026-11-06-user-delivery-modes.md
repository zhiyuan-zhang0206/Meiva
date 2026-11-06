# Agent user delivery modes

## Scope

One user supervises dozens of concurrent agents. Routine assistant text must not
compete with decisions, results and milestones. A user can return after an
agent rewrites `to_user.md` or compacts its conversation and still find unread
messages and outstanding questions. The agent session holding the work context
owns suggested replies. Publishing a milestone must leave execution running.

## Prior art

The existing first-line visibility protocol separates conversation from
expandable Details. The existing notice receipt transaction couples an explicit
user answer to exactly one inbound message. Postgres unique keys and row locks
already provide deduplication and concurrent resolution. React Query invalidation
and the live `agent_updated` hint already refresh durable state after reconnect.

## Simplest sufficient design

Use one finite `user_delivery_mode` setting and one durable user-message table.
Keep the existing prefix mode as the default for compatibility; `naive` and `sdk`
are explicit per-agent or cluster-default choices. Preserve each message's mode
in checkpoint/SSE metadata, so a later configuration change does not reinterpret
history. No separate notification producer is needed for SDK publications.

A publication contains immutable content, a reply expectation and explicit
suggestions. Reading and resolving are independent state transitions. Mutable
workspace prose cannot delete a publication. The SDK reader retrieves preserved
messages after compaction. One guarded retry corrects an omitted SDK decision at
a natural end; publishing itself never interrupts or ends execution. A second
omission raises an explicit contract failure, ending the correction loop without
claiming a successful completion or inventing a user-facing result.

## Context and constraints

The human reads asynchronously and may answer several questions after multiple
milestones. Agents can continue independent work while waiting. Required replies
are attention state, not scheduler suspension or authorization. Suggested replies
are editable drafts, not statements already made by the user.

True constraints: preserve unread content; preserve user intent under uncertain
HTTP responses; do not terminate on publish; do not treat reading or dismissing
as consent. Deliberate constraints: one mode per agent, at most three suggestions,
100,000 characters per publication, and at most one natural-end correction.
The obsolete constraint is using `to_user.md` as the sole shared message store.
There is no requirement that working notes be private: Details are user-readable.

## Decision

- `naive`: ordinary assistant text is visible without a marker.
- `prefix`: exact `MEIVA_DISPLAY_TO_USER` / `MEIVA_HIDE_FROM_USER` first lines;
  invalid output remains in expandable Details and receives corrective feedback.
- `sdk`: `meiva.ui.publish` is the delivery entry. Ordinary assistant text,
  including final summaries, stays in Details. Legacy notify is refused and the
  independent reply-suggestion model is bypassed. Publications automatically
  contribute unread and required-reply counts to the sidebar.

The publication key is mandatory and scoped to the authenticated principal and
agent. Same-key/same-payload retries return the existing record; changed payloads
conflict. Read/answer/dismiss reuse the existing resolution receipt transaction;
an answer commits its inbound message atomically. Publication bodies never change.
There is no implicit expiry or replacement of unread records.

Pending context supplied to the model contains at most 20 excerpts of 800
characters, with explicit truncation and a reader instruction. The SDK reader
paginates up to 100 records per request. This bounds recurring model input while
keeping full content durable. The selected frontend feed currently reads the
agent's complete publication history; a future UI pagination change must retain
access to pending and resolved messages.

## Alternatives considered

A prefix-only protocol fits normal model output but cannot reliably couple
question state and suggested replies to one message without another channel.
Structured fields on every ordinary text output would change the output contract
for every response. SDK delivery is opt-in so those behaviors remain comparable.

A notification after each text output would overwhelm the user. A second model
would lack the publishing session's context. Reusing the mutable workspace file
would retain the observed loss of unread information. Reusing the single-open
notice slot would prevent multiple milestones and independent questions.

Runtime-ending publish would make milestones prematurely end work. Automatically
promoting a final string after a missing SDK call would bypass explicit delivery
and reply semantics. Bounded corrective feedback preserves the distinction and
avoids an unbounded model loop.

## Consequences

Unread and unanswered content survives overview edits and compaction. SDK mode
requires an explicit publication or `publish(None, response="none", suggestions=[],
idempotency_key=...)` when there is no user update. The platform does not infer
which pending question an unrelated free-form chat message answers: the user
answers or closes the specific publication. Existing notice history remains
readable and resolvable after switching modes.
