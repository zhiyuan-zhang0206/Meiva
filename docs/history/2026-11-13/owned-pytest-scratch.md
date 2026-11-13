# Give each scoped pytest run its own scratch

The scoped check runner already owned a temporary JUnit directory, while pytest's
large basetemp belonged to an independent numbered generation. We placed basetemp
inside one run-owned scratch and kept the logical test command as the cache key,
so random locations cannot turn an unchanged selection into a cache miss.

A fixed shared scratch cleared at startup would let concurrent checks remove one
another's files. Each run instead removes only the directory it created, without
following symlinks, and reports incomplete removal. Failure diagnostics leave via
the existing output stream before scratch removal; the entire large test tree is
not retained for a small report.

Signal management belongs to this pytest owner, not the generic command runner.
TERM is deferred while the child handle is being established, then the owner
stops and waits for its child before cleanup. KILL has no finally guarantee; we
retain that boundary explicitly. The chosen directory shape reuses the existing
macmini reaper's supported root without changing or deploying a new reaper.
Postgres fixture ownership and general worktree garbage collection remain separate
responsibilities.

Source verification of the locked pytest version found that its numbered cleanup
also selects nonnumeric suffixes after `pytest-` (parsed as -1). The run directory
therefore uses `diff-checks-`, within the supported per-user root but outside
pytest numbered cleanup. The existing macmini reaper enumerates that root without
a filename-prefix filter; no reaper change is needed for this directory shape.


Independent review found three missing proofs in the initial implementation:
leader exit can leave a same-session descendant with closed output pipes; an
opened child directory needs owner and lstat/fstat identity checks before chmod;
and constructor failure must preserve a TERM deferred during startup. The revision
adds those gates. If the leader has exited but its group remains or cannot be
observed, scratch stays and cleanup fails visibly; no further signals are sent
without the leader anchor. Construction keeps the interrupt with the original
failure as its cause and restores the previous handler even without a child.


A further review control showed that cleanup failure could replace an already
received TERM with a generic error exit. The original signal interruption or
KeyboardInterrupt now remains the raised primary exception, with bounded cleanup
diagnostics and its typed cause preserving the incomplete-cleanup decision.
Without a primary exception, directory cleanup failure is an error. TERM received
during the bounded cleanup is recorded and resolved after restoring the caller's
handler, rather than discarded. These rules retain scratch on unknown child
state and preserve the original CLI interruption contract.
