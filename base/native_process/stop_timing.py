"""Host cancellation diagnostics: bounded unwind and activity-clock reads.

These bounds limit diagnostic work after explicit cancellation. Graceful
maintenance waits for durable drain and fails on timeout; it does not use an
automatic SIGKILL deadline. Registered in base/deploy/timing.py::CLOCKS.
"""

from __future__ import annotations

# How long a cancelled turn gets to unwind before the host reports it as
# uncancellable and stops waiting (services/agent_runner/agent_host/dispatcher.py).
CANCEL_UNWIND_TIMEOUT_S = 5.0

# Ceiling on reading the stuck agents' activity clocks to enrich that report
# (services/agent_runner/agent_host/dispatcher.py). Small on purpose: diagnostic enrichment
# of a report already worth emitting without it, and the DB may be exactly what
# a wedged turn is stuck on.
CLOCK_READ_TIMEOUT_S = 2.0

# meiva:note
# How long a SIGTERMed agent-host waits for its running turns to end at the restart each
# was given, the restart writes included; a turn still running at the deadline falls
# through to the cancel path. Registered in base/deploy/timing.py::CLOCKS like the bounds
# above. It must outlast a restart's interrupt grace, the interrupt poll and the unwind
# that follows the abort, or the graceful exit would cancel a turn its own restart was
# about to interrupt.
# meiva:link services/agent_runner/agent_host/lifecycle/docs/graceful-exit.meiva.okf.md
HOST_GRACEFUL_EXIT_DRAIN_S = 30.0

# meiva:note
# How long a pending restart waits before it interrupts the model call or code run it finds
# in flight: most code runs end well inside it, and a restart then takes effect at the
# turn's next boundary without cutting anything short. Cancel and terminate interrupt at
# once. The age is the restart row's own `created_at`, read by the interrupt watcher's poll
# (`agent.db.pending_interrupt_reason`).
# meiva:link agent/graph/docs/turn-interrupts.meiva.okf.md
RESTART_INTERRUPT_GRACE_S = 10.0

# meiva:note
# Poll cadence of the interrupt watcher: how soon an in-flight model call or code run
# notices a durable cancel, terminate or (once past its grace) restart. The watcher reads
# Postgres instead of sharing the claim node's Redis listener; see
# agent/graph/interrupt.py.
INTERRUPT_POLL_S = 2.0

# meiva:note
# The longest an agent-host's whole SIGTERM exit may run: the drain, then the scheduler's
# cancel unwind and clock read, the resource join (one more unwind bound), and the
# unbounded rest (runtimes, pools, health server) in what is left. The
# roster declares it as the unit's stop ceiling, so root's TERM window derives from it.
HOST_STOP_CEILING_S = 50.0

# meiva:note
# How long a SIGTERMed ops server waits for the requests it already admitted, and the executor
# work they started, before it cancels them (services/agent_runner/agent_ops/in_flight.py).
# New connections are refused from the moment SIGTERM arrives. Ops stops after the agent host,
# so what is left comes from the gateway or a remote runner and is short; a request still
# running at the deadline is named at ERROR and cancelled.
# meiva:link services/agent_runner/agent_ops/daemon.py
OPS_DRAIN_S = 15.0

# meiva:note
# The longest an ops server's whole SIGTERM exit may run: the drain, then the pool, the event
# pipeline and the server close, which carry no bound of their own. The roster declares it as
# the unit's stop ceiling, so root's TERM window derives from it.
OPS_STOP_CEILING_S = 25.0

# meiva:note
# Root's TERM window for a unit that declares a shutdown ceiling is that ceiling plus this
# margin (services/supervision/meiva_root_glue/manifests.py): the reap, the group-closure
# read and scheduling all happen inside root's window, so a unit that closes exactly at its
# own bound must still finish before root gives up on it.
STOP_MARGIN_S = 5.0
