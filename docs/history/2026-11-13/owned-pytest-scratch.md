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
