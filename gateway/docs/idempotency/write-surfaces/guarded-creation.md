# Guarded creation retry surfaces

These fixed versioned paths require a valid caller `Idempotency-Key`,
`Idempotency-Scope: principal-v1` and a verified principal. Same scoped key with
changed semantic request conflicts; different verified principals have
independent namespaces.

| Method and route | Retry policy | Transactional acceptance owner |
|---|---|---|
| `POST /api/keyed/v1/{guide,schedules,packages}/draft` | keyed | raw draft intent and immutable original birth/prompt/attempt |
| `POST /api/keyed/v1/agents` | keyed | original agent birth and first prompt |
| `POST /api/keyed/v1/agents/{agent_id}/retry-launch` | keyed | observed attempt, original replacement attempt and pointer |
| `POST /api/keyed/v1/task-assignments` | keyed | original agent/task pair, assignment inbound and audit facts |

These routes use domain receipts, so the SDK retries an ambiguous failure under
the same key and the retry recovers the original acceptance. Acceptance is not
evidence of runner readiness or completed execution. Launch recovery and
current observations remain in their native owners, outside the acceptance
transaction. None of these routes makes an entire execute_code script atomic.

Current owners:
[Guarded Draft Agent Creation](../../../agents/docs/guarded-drafts.md),
[Guarded Agent Creation](../../../agents/docs/guarded-creation.md),
[Guarded launch retry acceptance](../../../agents/docs/launch-retry.md) and
[Guarded compound task assignment](../../../agents/task_assignment/README.md).
