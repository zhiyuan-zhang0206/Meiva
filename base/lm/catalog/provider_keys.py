# meiva:note
# The provider API keys a process holds, as one value.
#
# A composition root reads the process environment and the home's `.env` once into a
# `ProviderKeys` (`base.lm.factory.process_provider_keys`) and hands it to the
# builders; a provider plugin takes its key from `ctx.keys`, never from `os.environ`.

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

__all__ = ["ProviderKeys"]


# meiva:note
# The API keys by the env name their provider declares (`ProviderBinding.key_env`).
#
# An empty value is an absent key. The mapping is a read-only view of a private copy and
# `repr` shows names only, so a log line or traceback never carries a secret.
@dataclass(frozen=True)
class ProviderKeys:
    secrets: Mapping[str, str] = field(repr=False)

    def __post_init__(self) -> None:
        held = {name: value for name, value in self.secrets.items() if value}
        object.__setattr__(self, "secrets", MappingProxyType(held))

    def __repr__(self) -> str:
        return f"ProviderKeys(names={sorted(self.secrets)})"

    # meiva:note
    # The keys among `key_envs` that `environ` or, failing that, the home's `.env` values
    # `env_file` carry.
    #
    # The process environment wins: a split runner receives its keys through the bootstrap
    # into the environment, while the gateway profile keeps them out of it and holds them
    # only in the file.
    @classmethod
    def read(
        cls, key_envs: Iterable[str], *, environ: Mapping[str, str], env_file: Mapping[str, str]
    ) -> ProviderKeys:
        return cls({name: environ.get(name) or env_file.get(name, "") for name in key_envs})

    def present(self, key_env: str) -> bool:
        return key_env in self.secrets

    # meiva:note
    # The key named `key_env`; `RuntimeError` when it is absent, so a missing key surfaces
    # at build time instead of as a provider 401 mid-turn.
    def require(self, key_env: str) -> str:
        key = self.secrets.get(key_env)
        if not key:
            raise RuntimeError(
                f"{key_env} not set — this provider needs its key; "
                "configure in ~/.meiva/.env or export before starting"
            )
        return key
