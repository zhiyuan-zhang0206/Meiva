"""Schedule-supervision timing — one lattice family.

The gateway's schedule manager raises a missing-session alert when an enabled,
non-completed schedule has gone sessionless for too long. The alert threshold
must outlive a legitimate cluster rollout's no-progress window: otherwise an
ordinary stop-the-world rollout would produce a schedule-stalled alert before
the updater itself is judged stuck.

Registered in `base/deploy/timing.py::CLOCKS` with the
`NO_PROGRESS_TIMEOUT_S < SCHEDULE_STALL_ALERT_AFTER_S` constraint;
`meizel/owners/clock_lattice.py` treats this module as a family module, so
lattice vocabulary may live here and only here.

Deliberately separate from `base/deploy/timing.py`: the schedule manager runs under
the gateway process profile, and importing the lattice module would drag its
agent/sandbox-domain settings reads into the gateway closure
(cli/commands/lifecycle/tests/startup/test_gateway_consumer_guard.py enforces the matrix).
"""

from __future__ import annotations

# How long an enabled, non-completed schedule may remain sessionless before
# the manager raises the stall alert (services/wake/schedule_manager/manager.py). Must stay
# above NO_PROGRESS_TIMEOUT_S — see the module docstring for why.
SCHEDULE_STALL_ALERT_AFTER_S = 2 * 60 * 60

# meiva:note
# How far past a resident schedule's own `alive_by` the manager waits before it calls the
# round late. It absorbs the reconcile poll (POLL_INTERVAL_S = 5 in
# services/wake/schedule_manager/manager.py) plus one late wake-up -- never a whole round.
# The writer knows how long its own round may take and states that in `alive_by`, so widening
# this constant would only hide slow rounds behind a bigger number instead of making the
# schedule declare them.
SCHEDULE_PROGRESS_SKEW_S = 60.0

# meiva:note
# What a resident loop promises before it has measured a round of its own. The first beat says only
# "the next round starts at once and will be reported within this budget"; every later beat replaces
# it with the slowest round the loop has actually taken, so no schedule has to declare a number a
# reader must then trust. Five minutes keeps a long first round -- a daily job that happens to fire
# on the same restart that launched the loop -- from reporting itself late, while a process that
# died before its second beat is still named inside the hour.
SCHEDULE_FIRST_ROUND_BUDGET_S = 5 * 60

# meiva:note
# How long after a session starts its runner record may still name the process that was killed to
# make room for it. The manager reaps a runner with a forced kill, so that process never clears its
# own record -- by design, "a record left behind is the process that did not say goodbye". A new
# runner writes its record within about two seconds of the session starting (measured 2026-11-06:
# session at 11:10:31.754, run row at 11:10:33). A minute is generous for a loaded host and still
# names a runner that never came up: past it, a record naming a gone pid is the death it looks
# like, so a session whose runner never started is not accepted forever.
SCHEDULE_LAUNCH_GRACE_S = 60.0
