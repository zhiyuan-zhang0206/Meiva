# meiva:note
# Stop timing for exec-host: the ceiling the supervisor gives SIGTERM before
# escalating to SIGKILL, and the internal phases the service takes to drain active runs.

from __future__ import annotations

SHUTDOWN_TERM_WAIT_S = 3.0
SHUTDOWN_KILL_S = 2.0
SHUTDOWN_CEILING_S = 10.0
