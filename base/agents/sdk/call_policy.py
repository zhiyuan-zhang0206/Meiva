"""Live SDK-event sampling, refreshed without network I/O on the SDK call path."""

from __future__ import annotations

import contextlib
import math
import random
import threading
import time
from collections.abc import Callable, Generator
from types import TracebackType

from base.agents.sdk import (
    InertSamplingPolicy as InertSamplingPolicy,
)
from base.agents.sdk import (
    SamplingPolicy as SamplingPolicy,
)
from base.agents.sdk import (
    SamplingPolicyLike as SamplingPolicyLike,
)
from base.agents.sdk import (
    SamplingSource as SamplingSource,
)
from base.agents.sdk import (
    keep_sample,
)
from base.agents.sdk import (
    policy_from_aliases as policy_from_aliases,
)
from base.log import logger


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
# the SDK call path; `rng` is the process root's random generator, which the owner draws each
# call's sampling decision from. The installation's root builds all three.
class SamplingPolicyOwner:
    def __init__(
        self, *, initial: SamplingPolicy, source: SamplingSource, rng: random.Random
    ) -> None:
        self.source = source
        self.rng = rng
        self.value = initial
        self.next_refresh = time.monotonic() + source.refresh_seconds
        self.lock = threading.Lock()
        self.refreshing = False
        self.error: tuple[Exception, TracebackType | None] | None = None
        self.worker: _Refresh | None = None
        self.stopped = False

    def should_sample(self, every: int) -> bool:
        if every == 1:
            return True
        return keep_sample(every, self.rng.random())

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
def process_sampling_owner(rng: random.Random) -> SamplingPolicyOwner:
    from base.host.env.dotenv_file import process_env_file
    from base.host.env.registry import process_environment

    env = process_environment()
    return SamplingPolicyOwner(
        initial=policy_from_aliases(env),
        source=SamplingSource(env=env, env_path=process_env_file()),
        rng=rng,
    )
