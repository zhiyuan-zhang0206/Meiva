# Schedule mutation receipts

Declarative mutations update durable timing and lifecycle state directly. The
manager observes that state on its next reconcile.

`PUT /api/schedules/{id}` and `POST .../start`, `.../stop`, `.../restart`
accept an optional `Idempotency-Key` (1–128 characters). The required
`Idempotency-Scope: principal-v1` binds a key to the verified credential and
route; a key without the scope, an unsupported scope, or a scope without a key
returns 400. Unkeyed requests remain separate intents and cannot be
automatically retried safely. The CLI and browser mint one key per invocation and
send it with the scope; retry code must reuse that key and immutable request. No
client outbox is involved.

## Acceptance transaction

The schedule owner reserves or locks a row in `schedule_operation_receipts`,
compares the immutable target and body, locks the schedule, changes its durable
state, and saves the original response in one transaction. Concurrent same-key
requests return that response; changed payloads return 409. Replay is checked
before current enabled state or existence, so an old restart receipt cannot
re-enable a schedule subsequently stopped or deleted. Receipts have no automatic
expiry. They report durable acceptance, independently of script completion.

Same-value edits add no version. A real script or command change writes a version
snapshot and takes effect on the next fire. Placement changes and deletion wait
until an open run closes. Legacy queued sync requests are drained by the elected
manager; they perform no resident-session actions.

See [router contracts](router-contracts.md) for lifecycle verbs and
[Schedule Manager & Runner](schedules.md) for execution semantics.
