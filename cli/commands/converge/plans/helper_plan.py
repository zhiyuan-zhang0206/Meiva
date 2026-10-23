# meiva:note
# What the permissions-helper steps do on this host.
#
# Pure decisions over the platform, the `permissions_helper_enabled` setting and the host's
# helper incapability reason (None when a helper can run here). The shells call the helper
# bring-up, ask the helper for its grants or clear the recorded answers.

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "BringUpHelper",
    "NoHelperPlatform",
    "RefuseBringUp",
    "helper_bring_up_plan",
    "helper_present",
]


# meiva:note
# A platform without a permissions helper: no bring-up.
@dataclass(frozen=True)
class NoHelperPlatform: ...


# meiva:note
# A macOS host that cannot run the mandatory helper: converge fails with this message.
@dataclass(frozen=True)
class RefuseBringUp:
    message: str


# meiva:note
# A macOS host that can run the helper: build, sign and load it.
@dataclass(frozen=True)
class BringUpHelper: ...


# meiva:note
# The bring-up of the stable signed ancestor a macOS root requires.
def helper_bring_up_plan(
    *, system: str, enabled: bool, incapability: str | None
) -> NoHelperPlatform | RefuseBringUp | BringUpHelper:
    if system != "darwin":
        return NoHelperPlatform()
    if not enabled:
        return RefuseBringUp("macOS root supervision requires the permissions helper")
    if incapability is not None:
        return RefuseBringUp(f"macOS root supervision cannot start: {incapability}")
    return BringUpHelper()


# meiva:note
# Whether a helper can exist on this host, so asking it a question is meaningful.
#
# On a host that cannot run one the bring-up step already said so; a derived second
# complaint from a step that depends on the helper would be noise rather than news.
def helper_present(*, enabled: bool, incapability: str | None) -> bool:
    return enabled and incapability is None
