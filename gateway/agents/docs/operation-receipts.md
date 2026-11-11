# Question Operation Receipts

Asking (`POST /api/agents/{id}/asks`), closing (`POST /api/agents/{id}/asks/{question_id}/close`) and
answering (`POST /api/keyed/v1/agents/{id}/questions/answers`) require `Idempotency-Key` and
`Idempotency-Scope: principal-v1` headers. `keyed_operation_receipts` owns each normalized request
and its immutable result, scoped by concrete HTTP path and the credential-aware key. The receipt and
all question and reply mutations commit together. Concurrent retries serialize in Postgres; changed
inputs conflict (409). Missing keys fail before mutation. Replay of the same key returns its
original result.

An ask receipt keeps the original ids, in order; a replay returns them and never inserts a second
row. An absent Note suggestion stays absent from serialized questions, so adding the optional field
never changes request equality or accepted retries. Every ask, close and answer locks the agent row,
so they serialize per agent.

An answer receipt holds its inbound id. Retry repairs its wake tail, with the exact pending-row
resurrection guard preventing revival of completed work. A submission that originally succeeded
replays instead of returning the open-set conflict. A submission whose `expected_ids` differ from the
agent's open set writes no receipt (nothing was settled); the 409 carries the latest open set and the
caller submits again under a new key.

Receipts have no TTL pruning: expiration cannot turn a known operation into a new side effect. The
transport uses a runtime-owned operation identity and retries an ambiguous outcome under the same
key. The same table serves the publications' seen cursor and the reply-suggestion feedback.
