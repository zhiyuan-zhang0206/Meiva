# meiva:note
# The typed surface the boot-lite state machine (`base/config/_lite.py`) holds over the eager
# `Settings` aggregate it forwards to.
#
# The aggregate and its domain sub-models are not imported here (that would build the eager chain a
# lite process defers), so the owner reads them through what this module states: the Protocol view of
# the aggregate, the attribute read that hands back `object`, and the checks that narrow what a read
# returned.

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, TypedDict, TypeGuard

from base.host.env.dotenv_boot import EnvBootResult

__all__ = [
    "BootState",
    "EagerSettings",
    "InstalledBundle",
    "explicitly_provided",
    "is_eager_settings",
]


class EagerSettings(Protocol):
    @property
    def profile(self) -> str | None: ...

    env_boot: EnvBootResult

    def has_domain(self, name: str) -> bool: ...


class InstalledBundle(Protocol):
    @property
    def settings(self) -> EagerSettings: ...

    @property
    def exports(self) -> Mapping[str, object]: ...


# meiva:note
# The boot-lite state machine snapshot `boot_state()` hands to tests and acceptance runs.
class BootState(TypedDict):
    mode: str
    reason: str | None
    upgrades: int
    pending_count: int
    prepared: bool


def _is_object_set(value: object) -> TypeGuard[set[object]]:
    return isinstance(value, set)


# meiva:note
# Whether `name` is in the `model_fields_set` a pydantic sub-model reported (read by the caller, which
# owns the attribute access).
def explicitly_provided(model_fields_set: object, name: str) -> bool:
    if not _is_object_set(model_fields_set):
        raise TypeError("model_fields_set is not a set")
    return name in model_fields_set


# meiva:note
# Whether `value` carries the surface of a constructed eager aggregate. The facade holds the lite
# view under `settings` until the upgrade installs the real object.
def is_eager_settings(value: object) -> TypeGuard[EagerSettings]:
    return hasattr(value, "has_domain") and hasattr(value, "env_boot")
