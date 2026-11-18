# Cron slot claims

Cron schedules store their expression and next eligible UTC fire on the schedule
row. Admission claims the due slot and opens a `schedule_runs` row in one
transaction. An open run prevents a second process for the same schedule.

The payload receives the claimed slot as `MEIVA_SCHEDULED_FOR`; it performs one
round and exits. Recurring advancement uses the later of the slot and current
time, so recovery catches up once and resumes cadence without replaying every
historical slot. The old script-local cron claim helper is retired.

See [Schedule Manager & Runner](schedules.md) for admission and settlement.
