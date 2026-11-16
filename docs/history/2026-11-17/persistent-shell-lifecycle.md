# Close persistent shells at every terminal agent transition

Task #7085 closes the gap between an agent reaching `terminated` and the
persistent PTY sessions and TTL rows that used to survive it. A terminal status
now has one lifecycle meaning: sessions created before that transition are
closed on the agent's home machine and their `agent_shell_ttls` rows are removed
only after the close has a definitive result.

The normal graceful host path, force path, already-terminated retry,
harness-transition path, and crash-corpse path each capture the current
`session_index` under their status-transition lock. The exclusive cutoff lets
cleanup close exactly the sessions that existed at termination without chasing a
later allocation. Session allocation predicates on the same terminal state, so a
concurrent allocation is either included below that cutoff or refused after the
terminal transition. When the immediate host-side close cannot run or confirm its
result, its TTL rows remain so the cluster TTL reaper can retry safely.

The reaper now treats `agents_meta.status = 'terminated'` as a reclamation
signal independent of expiry. It issues the normal home-machine `shell_kill`
operation and deletes only the exact selected row after `killed`, `absent`, or
`machine_absent`. It also reconciles unexpired orphan rows every remote round,
after a five-minute creation grace, instead of delaying inspection for hours.

New system watcher sessions are capped at 72 hours (`259200` seconds), while
ordinary shell sessions retain their stricter 24-hour cap. The cap applies at
creation and renewal boundaries. Stored rows with historical longer expiries
remain readable and can be renewed to a permitted new deadline, allowing the
later data migration to proceed without a read-path outage.

Update (2026-11-17): the allocation predicate described above did not by itself
cover the interval between committing a session id and completing PTY creation.
The corrected creation-atomicity decision is recorded in
[Persistent shell creation fence](persistent-shell-creation-fence.md).
