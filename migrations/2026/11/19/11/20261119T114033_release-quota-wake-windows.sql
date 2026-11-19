-- An external harness that runs out of quota no longer parks its agent behind a
-- `wake_suppressed_until` deadline parsed from the provider's reset text: it halts the agent
-- through the recovery breaker (`permanent_reject_streak`), and a new message resumes it.
-- No code reads a 'quota_exhausted' window any more, so the windows the previous code stamped
-- (some days long) are released here; a stale one would otherwise read as a stranded-wake
-- cooldown. The agents they held wake for their pending messages, and one that is still out of
-- quota halts again on its next turn.
--
-- Idempotent: a fresh database has no such rows, and a replay finds none.
UPDATE agents_meta
SET wake_suppressed_until = NULL, wake_suppress_reason = NULL
WHERE wake_suppress_reason = 'quota_exhausted';
