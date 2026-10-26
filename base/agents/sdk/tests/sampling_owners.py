import random
from pathlib import Path

from base.agents.sdk.call_policy import (
    SamplingPolicy,
    SamplingPolicyOwner,
    SamplingSource,
    policy_from_aliases,
)
from base.clock.tests.seeded_rng import seeded_rng
from base.host.env.boot_policy import BootstrapFetch

__all__ = [
    "fixed_sampling_owner",
    "gateway_sampling_owner",
    "sampling_owner_failing_to_read",
    "sampling_owner_over",
    "sampling_owner_serving",
]

_NO_ENV_FILE = Path("/nonexistent-sampling-home/.env")


# meiva:note
# A sampling owner whose policy is the default and stays the default, drawing its decisions from
# `rng`: it reads no gateway, and the `.env` path it refreshes from names no file, so the SDK call
# path under test sees live sampling without a network call, a settings read or a file. A test
# about the generator hands in its own; the others use `fixed_sampling_owner`.
def sampling_owner_over(rng: random.Random) -> SamplingPolicyOwner:
    return SamplingPolicyOwner(
        initial=SamplingPolicy(),
        source=SamplingSource(env={}, env_path=_NO_ENV_FILE),
        rng=rng,
    )


# meiva:note
# `sampling_owner_over` the seeded generator every test shares: repeatable decisions.
def fixed_sampling_owner() -> SamplingPolicyOwner:
    return sampling_owner_over(seeded_rng())


# meiva:note
# A sampling owner that serves `policy` from a real `.env` file at `env_path` (written here), the
# way a local-config process does: its first snapshot and every refresh are that policy until the
# file is edited.
def sampling_owner_serving(
    policy: SamplingPolicy, env_path: Path, *, refresh_seconds: float = 5.0
) -> SamplingPolicyOwner:
    env_path.write_text(
        f"MEIVA_SDK_CALL_SAMPLING_ENABLED={str(policy.sampling_enabled).lower()}\n"
        f"MEIVA_SDK_CALL_SAMPLE_EVERY={policy.sample_every}\n"
    )
    return SamplingPolicyOwner(
        initial=policy,
        source=SamplingSource(env={}, env_path=env_path, refresh_seconds=refresh_seconds),
        rng=seeded_rng(),
    )


# meiva:note
# A sampling owner of an enrolled process: each refresh asks the gateway at `gateway_url` with a
# machine token, within `timeout_s`. `initial` is the policy served until the first refresh lands.
def gateway_sampling_owner(
    gateway_url: str,
    env_path: Path,
    *,
    initial: SamplingPolicy | None = None,
    refresh_seconds: float = 0.0,
    timeout_s: float = 5.0,
) -> SamplingPolicyOwner:
    env = {
        "MEIVA_MACHINE_SERVE_AGENT_RUNNER": "true",
        "MEIVA_GATEWAY_URL": gateway_url,
        "MEIVA_API_TOKEN": "machine-token",
    }
    return SamplingPolicyOwner(
        initial=policy_from_aliases(env) if initial is None else initial,
        source=SamplingSource(
            env=env,
            env_path=env_path,
            tuning=BootstrapFetch(timeout_s=timeout_s, attempts=1),
            refresh_seconds=refresh_seconds,
        ),
        rng=seeded_rng(),
    )


# meiva:note
# A sampling owner whose every refresh fails with a real read error: its `.env` file is not valid
# UTF-8, which the file read refuses (`UnicodeDecodeError`, not a transient outage, so the owner
# keeps the failure and rejects the next SDK entry with it).
def sampling_owner_failing_to_read(
    env_path: Path, *, refresh_seconds: float = 0.0
) -> SamplingPolicyOwner:
    env_path.write_bytes(b"MEIVA_SDK_CALL_SAMPLE_EVERY=\xff\xfe\n")
    return SamplingPolicyOwner(
        initial=SamplingPolicy(),
        source=SamplingSource(env={}, env_path=env_path, refresh_seconds=refresh_seconds),
        rng=seeded_rng(),
    )
