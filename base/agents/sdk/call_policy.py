"""Live SDK-event sampling, refreshed without network I/O on the SDK call path."""

from __future__ import annotations

import contextlib
import math
import threading
import time
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType

from pydantic import BaseModel, Field

from base.host.env.boot_policy import BootstrapFetch
from base.log import logger


class SamplingPolicy(BaseModel):
    """Sampling is opt-in; the ratio is the inverse inclusion probability."""

    sampling_enabled: bool = False
    sample_every: int = Field(default=10, ge=1)


# meiva:note
# Where the SDK sampling policy is read from, and how often.
#
# `env` is the launch environment and `env_path` the `.env` file of the home: a process whose
# config source is local reads the file, any other asks its gateway with the environment's URL
# and machine token (`read_policy`). `env` is a mapping the root hands in, so a root that passes
# the live process environment (`process_environment()`) sees a token or flag that appears later,
# and a test passes a dict. `tuning` bounds the gateway fetch; `refresh_seconds` is the wait
# between fetches, counted from the previous one (the first fetch is due that long after the owner
# is built).
@dataclass(frozen=True)
class SamplingSource:
    env: Mapping[str, str]
    env_path: Path
    tuning: BootstrapFetch = field(
        default_factory=lambda: BootstrapFetch(timeout_s=2.0, attempts=1)
    )
    refresh_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.refresh_seconds) or self.refresh_seconds < 0:
            raise ValueError("SDK sampling refresh_seconds must be finite and nonnegative")


# meiva:note
# The sampling policy `aliases` (env or `.env` values by alias) say; an alias that is absent takes
# the default, and a value outside the policy's bounds raises `ValidationError`.
def policy_from_aliases(aliases: Mapping[str, str]) -> SamplingPolicy:
    defaults = SamplingPolicy()
    return SamplingPolicy.model_validate(
        {
            "sampling_enabled": aliases.get(
                "MEIVA_SDK_CALL_SAMPLING_ENABLED", defaults.sampling_enabled
            ),
            "sample_every": aliases.get("MEIVA_SDK_CALL_SAMPLE_EVERY", defaults.sample_every),
        }
    )


# meiva:note
# One fetch of the cluster's SDK sampling policy from `source`: the `.env` file when this
# process's config source is local, the gateway otherwise. The file is read again on each call
# so an edit reaches a running process within one refresh.
def read_policy(source: SamplingSource) -> SamplingPolicy:
    from base.host.env.bootstrap import (
        config_source_is_local,
        fetch_bootstrap_config,
        should_fetch_from_gateway,
    )
    from base.host.env.dotenv_file import read_env_aliases
    from base.host.net.auth import delivered_token

    env = source.env
    env_file = read_env_aliases(source.env_path)
    if config_source_is_local(env, env_file=env_file) or not should_fetch_from_gateway(
        env, env_file=env_file
    ):
        aliases = env_file
    else:
        aliases = fetch_bootstrap_config(
            env["MEIVA_GATEWAY_URL"], bearer=delivered_token(env), tuning=source.tuning
        )
    return policy_from_aliases(aliases)


class _Refresh:
    """One retained refresh attempt; shutdown observes the actual worker."""

    def __init__(self, refresh: Callable[[], None]) -> None:
        self.refresh = refresh
        self.stopping = threading.Event()
        self.completed = threading.Event()
        self.error: BaseException | None = None
        self.traceback: TracebackType | None = None
        self.thread = threading.Thread(target=self.run, name="sdk-call-policy", daemon=True)
        self.thread.start()

    def run(self) -> None:
        try:
            if not self.stopping.is_set():
                self.refresh()
        except BaseException as exc:
            self.error = exc
            self.traceback = exc.__traceback__
            logger.bind(_no_emitter=True).opt(exception=exc).error(
                "SDK sampling refresh worker failed: {error}", error=exc
            )
        finally:
            self.completed.set()

    def stop(self, timeout: float) -> bool:
        self.stopping.set()
        self.thread.join(timeout=timeout)
        if self.thread.is_alive():
            return False
        if self.error is not None:
            self.error.with_traceback(self.traceback)
            raise self.error
        return True


# meiva:note
# The SDK installation's policy snapshot and the finite lifetime of its refresh. `initial` is the
# snapshot served until the first fetch lands; `source` says where each refresh reads from, off
# the SDK call path. The installation's root builds both.
class SamplingPolicyOwner:
    def __init__(self, *, initial: SamplingPolicy, source: SamplingSource) -> None:
        self.source = source
        self.value = initial
        self.next_refresh = time.monotonic() + source.refresh_seconds
        self.lock = threading.Lock()
        self.refreshing = False
        self.error: tuple[Exception, TracebackType | None] | None = None
        self.worker: _Refresh | None = None
        self.stopped = False

    def read(self) -> SamplingPolicy:
        with self.lock:
            if self.stopped:
                raise RuntimeError("the SDK sampling owner has stopped")
            if self.worker is not None and self.worker.error is not None:
                raise self.worker.error.with_traceback(self.worker.traceback)
            alive = self.worker is not None and self.worker.thread.is_alive()
            if time.monotonic() >= self.next_refresh and not self.refreshing and not alive:
                self.refreshing = True
                try:
                    self.worker = _Refresh(self.refresh)
                except BaseException:
                    self.refreshing = False
                    raise
            if self.worker is not None and self.worker.error is not None:
                raise self.worker.error.with_traceback(self.worker.traceback)
            if self.error is not None:
                error, traceback = self.error
                raise error.with_traceback(traceback)
            return self.value

    def stop(self, timeout: float = 2.0) -> bool:
        """Fence new reads; return False while the retained attempt remains alive.

        A later stop observes its original failure. A completed invalid refresh
        raises the same exception that subsequent SDK entries would receive.
        """
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("SDK sampling stop timeout must be finite and nonnegative")
        with self.lock:
            self.stopped = True
            worker = self.worker
        if worker is not None and not worker.stop(timeout):
            return False
        with self.lock:
            if self.error is not None:
                error, traceback = self.error
                raise error.with_traceback(traceback)
        return True

    def close(self) -> bool:
        """Report an unfinished finite stop; propagate completed original failures."""
        joined = self.stop()
        if not joined:
            logger.bind(_no_emitter=True).warning(
                "SDK sampling refresh unfinished at shutdown; worker retained",
                event="sdk_sampling_refresh_unfinished",
            )
        return joined

    @contextlib.contextmanager
    def execution(self) -> Generator[None, None, None]:
        """Collect refresh outcomes before the child forms its result envelope."""
        primary: BaseException | None = None
        primary_traceback: TracebackType | None = None
        try:
            yield
        except BaseException as exc:
            primary, primary_traceback = exc, exc.__traceback__
            raise
        finally:
            try:
                self.close()
            except BaseException as secondary:
                if primary is None:
                    raise
                if secondary is not primary:
                    primary.add_note(f"SDK sampling shutdown also failed: {secondary!r}")
                primary.with_traceback(primary_traceback)

    def refresh(self) -> None:
        transient_errors: tuple[type[Exception], ...] = ()
        status_error = None
        try:
            import httpx

            transient_errors = (
                httpx.TimeoutException,
                httpx.NetworkError,
                httpx.RemoteProtocolError,
            )
            status_error = httpx.HTTPStatusError
            value = read_policy(self.source)
            with self.lock:
                self.value = value
                self.error = None
        except Exception as exc:
            expected = isinstance(exc, transient_errors) or (
                status_error is not None
                and isinstance(exc, status_error)
                and exc.response.status_code == 429
            )
            if expected:
                logger.bind(_no_emitter=True).opt(exception=True).warning(
                    "SDK sampling config fetch unavailable; retaining the last valid policy",
                )
            else:
                with self.lock:
                    self.error = (exc, exc.__traceback__)
                logger.bind(_no_emitter=True).opt(exception=True).error(
                    "SDK sampling config refresh failed; SDK calls will reject the invalid policy",
                )
        finally:
            with self.lock:
                self.next_refresh = time.monotonic() + self.source.refresh_seconds
                self.refreshing = False


# meiva:note
# The sampling owner of a process whose root has no configuration boot of its own (a bare SDK
# process, an exec child, a CLI verb): the composition root reads the process environment and
# the home's `.env` location here, once, as values. The environment is the live process
# environment, so a flag or token delivered after install is seen at the next refresh. Until the
# first refresh the owner serves the policy the environment already carries.
def process_sampling_owner() -> SamplingPolicyOwner:
    from base.host.env.dotenv_file import process_env_file
    from base.host.env.registry import process_environment

    env = process_environment()
    return SamplingPolicyOwner(
        initial=policy_from_aliases(env),
        source=SamplingSource(env=env, env_path=process_env_file()),
    )
