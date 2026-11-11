# Notice Operation Receipts

Question creation (`POST /api/agents/{id}/asks`) and notice resolution require `Idempotency-Key` and
`Idempotency-Scope: principal-v1` headers.
`notice_operation_receipts` owns each normalized request and its immutable
result, scoped by concrete HTTP path and the credential-aware key. The receipt
and all notice/reply mutations commit together. Concurrent retries serialize
in Postgres; changed inputs conflict (409). Missing keys fail before mutation. Replay of the same key returns its original inbound. A fresh read operation is rejected.

A resolution receipt holds its original optional inbound id. Retry repairs
its wake tail, with the exact pending-row resurrection guard preventing revival
of completed work. An answer or dismissal which originally succeeded replays
instead of returning an already-resolved conflict. Historical read receipts still replay; they do not admit a new read operation.

An ask receipt keeps the original question id; a replay returns it and never
inserts a second row. Receipts written by the removed notice-creation route stay
in the table as inert history. An absent Note suggestion stays absent from
serialized questions, so adding the optional field never changes historical
request equality or accepted retries. Every ask locks the agent row to serialize
local id allocation.

Receipts have no TTL pruning: expiration cannot turn a known operation into a
new side effect. Any future retention design needs explicit expired-key
semantics. The transport uses a runtime-owned operation identity and retries an
ambiguous outcome under the same key.

Browser resolution uses the fixed required-key/principal route
`POST /api/keyed/v1/agents/{id}/notices/{notice_id}/resolve`. It reuses this same
transaction owner, scoped to its own concrete path; keys are not
interchangeable across paths. The API accepts a caller operation
id or allocates one per invocation. The mounted reply component retains the
same key for a manual retry of an unchanged action/reply/target after failure;
changed input starts a new intent. It does not persist or automatically retry
requests, and an unsupported server is refused without fallback.
CLI and IM bridge resolution use the same route, sending one fresh key and the
`principal-v1` scope per invocation; they remain single attempts.

## Structured question resolutions

Question rows carry immutable question JSON.
Answer resolution validates the submitted selection against the locked question,
canonicalizes selected IDs into its original number order, commits answer JSON
(an empty selection with a comment is a complete answer; one with neither is
refused),
readable reply, notice state, inbound and operation receipt together. Text-only
clients use the same validator after parsing the numbered first line. Numbers inside the optional comment never select an option. Replay keeps the original inbound and
answer without validating against later state.
Legacy operation normalization omits absent question/answer fields so old accepted
receipt requests retain their equality across this additive schema upgrade.

## Provenance of a notice write

`agent_notices.source_verified_by` records the credential the auth middleware
verified for the request that wrote the row — the same fact vocabulary inbound
messages already store (`cluster_bearer`, `machine_token:<class>`,
`user_session`, `machine_session:runner`, `trusted_network`). The path's
`agent_id` is the caller's own claim and is deliberately not copied there; the
row therefore answers "which credential posted this" rather than "which agent
the caller said it was". In-process writers that present no credential at all
(task maintenance) write the literal `internal`, and rows written before the
column existed keep `NULL`. The value rides the notice feed
(`GET /api/notices` and `/open`) and `meiva notices list`.
