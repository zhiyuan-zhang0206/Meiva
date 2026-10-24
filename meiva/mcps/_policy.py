# meiva:note
# What one MCP daemon runs with, and when it retries, as values. `main` reads the process once and builds the `DaemonConfig`; every function below the entry point takes it (or one field of it). The retry rule is a function of the attempt number and whether the failure was a transport error, so the request loop only sleeps for the delay this module returns.
# meiva:link meiva/mcps/docs/mcps.meiva.okf.md
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


# meiva:note
# Bounded exponential backoff between the tries of one request: the delay after failed try `n` (0-based) is `base_delay_s * 2**n`, capped at `max_delay_s`; the last allowed try ends the request.
@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_s: float = 1.0
    max_delay_s: float = 8.0


# meiva:note
# The settings and facts the daemon reads once at start. `connect_timeout_seconds` bounds a stdio or static-auth connect; `display_available` is the host fact a server's `requires.display` is checked against; `home` and `browser_cdp_port` locate the per-machine browser and computer services; `project_root` and `home` scope which stale daemons this one reaps; `socket_probe_timeout_seconds` bounds the live-socket probe.
@dataclass(frozen=True)
class DaemonConfig:
    connect_timeout_seconds: float
    display_available: bool
    home: Path
    project_root: Path
    browser_cdp_port: int
    socket_probe_timeout_seconds: float = 1.0
    retry: RetryPolicy = field(default_factory=RetryPolicy)


# meiva:note
# Seconds to wait before the next try after failed try `attempt` (0-based), or None when the request ends with that failure: a failure that is not a transport error never retries, and the last allowed try never waits.
def retry_delay(policy: RetryPolicy, attempt: int, *, transport_error: bool) -> float | None:
    if not transport_error or attempt + 1 >= policy.max_attempts:
        return None
    return min(policy.base_delay_s * 2.0**attempt, policy.max_delay_s)
