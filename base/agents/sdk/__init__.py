# meiva:note
# Pure SDK sampling policy models and protocols.

from __future__ import annotations

import contextlib
import math
import threading
from collections.abc import Generator, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from base.host.env.boot_policy import BootstrapFetch


# meiva:note
# Sampling is opt-in; the ratio is the inverse inclusion probability.
class SamplingPolicy(BaseModel):
    sampling_enabled: bool = False
    sample_every: int = Field(default=10, ge=1)


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


class SamplingWorkerLike(Protocol):
    @property
    def thread(self) -> threading.Thread: ...


class SamplingPolicyLike(Protocol):
    stopped: bool

    @property
    def worker(self) -> SamplingWorkerLike | None: ...

    def should_sample(self, every: int) -> bool: ...

    def read(self) -> SamplingPolicy: ...

    def stop(self, timeout: float = 2.0) -> bool: ...

    def close(self) -> bool: ...

    def execution(self) -> AbstractContextManager[None]: ...


# meiva:note
# Inert sampling policy owner for mounts before an SDK root is installed.
class InertSamplingPolicy:
    def __init__(self) -> None:
        self.value = SamplingPolicy()
        self.stopped = False
        self.calls = 0
        self.worker: SamplingWorkerLike | None = None

    def should_sample(self, every: int) -> bool:
        if every <= 1:
            return True
        self.calls += 1
        return self.calls % every == 0

    def read(self) -> SamplingPolicy:
        if self.stopped:
            raise RuntimeError("the SDK sampling owner has stopped")
        return self.value

    def stop(self, timeout: float = 2.0) -> bool:
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("SDK sampling stop timeout must be finite and nonnegative")
        self.stopped = True
        return True

    def close(self) -> bool:
        return self.stop()

    @contextlib.contextmanager
    def execution(self) -> Generator[None, None, None]:
        try:
            yield
        finally:
            self.close()


__all__ = [
    "InertSamplingPolicy",
    "SamplingPolicy",
    "SamplingPolicyLike",
    "SamplingSource",
    "policy_from_aliases",
]
