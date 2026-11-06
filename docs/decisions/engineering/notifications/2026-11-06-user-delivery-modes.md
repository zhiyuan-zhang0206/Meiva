# Publish, Ask and composer suggestions

## Scope

One user supervises dozens of agents. Preserve unread messages and unanswered
questions across overview rewrites and compaction. Publishing and asking never
end execution. The user can edit every suggested reply and every question Note.

## Prior art

The existing visibility protocol separates the timeline from expandable Details.
The notice store already preserves immutable questions, ordered options, answer
history and transactional inbound receipts. The composer suggestion service already
owns bounded model calls. Reuse these owners rather than introducing parallel queues.

## Simplest sufficient design

Keep `user_delivery_mode` (`naive`, `prefix`, `sdk`), defaulting to `prefix`.
`publish` stores information and optional agent-authored reply candidates. `ask`
creates a persistent question in the existing question store, with an optional
Note suggestion. Multiple questions may remain open. Ask replaces the agent-facing
Notification surface and its push delivery. The existing store and read/answer
contracts preserve pending questions and history during the transition.

Composer suggestions remain a separate service in all modes. Inputs include formal
messages, Agent candidates, open questions, explicit accepted/edited/rejected
feedback across agents, and relevant long-term memory. Context is bounded; feedback
is provenance-bearing evidence, never an instruction or proof of approval. Ignoring
a suggestion is not rejection. Model output is a user-editable draft.

## Context and constraints

The user reads asynchronously, may have several outstanding decisions, and owns
cross-agent priorities unavailable to any individual agent. True constraints:
read is not answer or consent; retries must not duplicate questions or answers;
working notes remain user-expandable; neither publish nor ask controls the scheduler.
Deliberate constraints: at most three publication candidates, 100,000 characters per
publication, bounded model context and one correction at a natural turn end.
The obsolete constraints are one open notification per agent, implicit expiry of
unanswered questions, and mandatory suggestions on every publication.

## Decision

- Naive displays ordinary assistant text. Prefix requires the exact display/Details
  marker. SDK folds ordinary assistant text, including final summaries, into Details.
- SDK completion requires publish, ask or `publish(None=...)`.
  One omitted-delivery correction is followed by an explicit contract failure.
- Publish has optional suggestions and unread state. It creates no question.
- Ask has a text/single-select/multi-select question and an editable Note for every
  type. `note_suggestion` is an Agent draft, not a submitted user answer. Answer or
  explicit close resolves a question; reading and overview edits do not.
- Legacy notification storage and answer receipts remain as compatibility data;
  the agent-facing API and product vocabulary become Ask. Pending legacy questions
  lose automatic expiry, and existing answers retain their original content.
- Agent-authored candidates feed the composer service. There is one composer
  suggestion presentation, with explicit feedback recorded separately from messages.

## Alternatives considered

Binding reply expectations and mandatory candidates to every publish conflates
information delivery, questions and drafting. A separate new question table duplicates
existing receipt and history behavior. A single model context cannot represent both
all task details and all user history; the drafting service combines bounded sources
with explicit provenance instead. Runtime-ending publish or ask would interrupt
milestone work. Notifications on every message would consume excessive attention.

## Consequences

Formal delivery, question state and drafting can evolve independently. Pending
questions survive compaction and are discoverable alongside the editable overview.
There is no deployment in this contribution. Internal legacy route/table names are
retained to preserve stored history and compatibility while the SDK and UI use Ask.
