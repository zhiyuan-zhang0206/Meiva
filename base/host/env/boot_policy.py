# meiva:note
# The pure decisions of the config boot: which flag spelling is on, how long a connect retry
# waits, what a bootstrap payload must look like, how a fetch is tuned.
#
# Values in, values out: nothing here reads the environment, a file, the clock or the network.
# `base.host.env.bootstrap` is the shell that reads them and executes the plan.


from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

__all__ = ["BootstrapFetch", "fetch_backoff", "serve_flag", "validated_bootstrap_payload"]

_TRUTHY = frozenset({"1", "true", "yes", "on"})


# meiva:note
# An agent boots by fetching this. The timeout must cover a slow-but-healthy
# fetch under load (the whole boot -- fetch + import + claim -- has to finish
# before the parent's launch-confirm poll gives up; too small a cap fails a
# fetch that would have succeeded -- a 3s cap regressed CI). The retry covers a
# gateway briefly unreachable mid-restart (connection refused), which fails fast.
# A ReadTimeout (slow response) is deliberately NOT retried: stacking another
# full timeout would blow past the launch-confirm window and fail a spawn whose
# first fetch would have eventually returned.
# Tuning of one bootstrap fetch; every default is the production value.
#
# The config boot passes `BootstrapFetch()`; a test passes small values.
# `backoff_step_s` is the linear connect-retry step (`fetch_backoff`), and
# `snapshot_fresh_s` how long a parent snapshot stands in for a live fetch: it
# bounds cluster-edit propagation to a few minutes in steady state (the first
# child past the window re-fetches and refreshes the snapshot) while keeping the
# common per-turn child boot fetch-free.
@dataclass(frozen=True)
class BootstrapFetch:
    timeout_s: float = 10.0
    attempts: int = 2
    backoff_step_s: float = 0.5
    snapshot_fresh_s: float = 300.0


# meiva:note
# Seconds to wait after failed attempt number `attempt` (0-based): linear in the attempt.
def fetch_backoff(attempt: int, step_s: float) -> float:
    return step_s * (attempt + 1)


# meiva:note
# Settings-free mirror of one capability flag's resolution: `env`
# `MEIVA_MACHINE_SERVE_*` > the home's `.env` values (`env_file`) > False.
#
# Normal config boot has already loaded the `.env` into the environment before
# this runs, so its value is in `env`. Settings-lite maintenance commands do
# not load it: they pass the file's values too, so `meiva config --local`
# correctly sees a gateway unit without constructing Settings.
# `base.cluster.machine` reads the flag through Settings, which does not exist
# yet while this module runs. A malformed value resolves False here; the real
# resolver raises on it loudly later, so the config-source decision can never
# silently disagree with the machine role.
def serve_flag(env_key: str, env: Mapping[str, str], env_file: Mapping[str, str]) -> bool:
    raw = env.get(env_key)
    if raw is not None and raw.strip():
        return raw.strip().lower() in _TRUTHY
    raw = env_file.get(env_key)
    if raw is not None and raw.strip():
        return raw.strip().lower() in _TRUTHY
    return False


# meiva:note
# Assert the bootstrap body is a flat ``{str: str}`` map before the caller
# writes it into the environment.
#
# A gateway version skew, a wrong endpoint, or a proxy substituting the body
# could feed a non-str value that either raises an opaque ``TypeError`` deep in
# ``os.environ.__setitem__`` or (for a str-able scalar) writes a value the
# recipient never round-trips. Fail loud at the trust boundary instead.
def validated_bootstrap_payload(payload: object) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise TypeError(f"/api/bootstrap returned {type(payload).__name__}, expected a JSON object")
    raw = cast("dict[str, Any]", payload)
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise TypeError(
                "/api/bootstrap must be a flat {str: str} map; got "
                f"{type(key).__name__} key -> {type(value).__name__} value for {key!r}"
            )
    return raw
