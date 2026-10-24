# meiva:note
# Put an `Installation` value (or a `Loading` marker) into the process slot for a `with` block and
# restore the previous holder afterwards. The slot owner's own test support: a test of a facade that reads the
# installation (the model catalog, the configuration authority, the clock) needs the process
# root to exist, and holds the value it built instead of replacing the slot's name.
# `install.install` is the real writer of a full surface; this holds a bare value only.
from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from base.agents.sdk.call_policy import SamplingPolicy, SamplingPolicyOwner
from meiva.sdk_surface import meiva_module
from meiva.sdk_surface.installation import SLOT, Installation, Loading, slot

__all__ = ["held_installation", "inert_sampling"]


# meiva:note
# A sampling owner for the installation a test builds: the real owner, serving the default policy
# and refreshing to the same default, so nothing reads the settings or the gateway.
def inert_sampling() -> SamplingPolicyOwner:
    return SamplingPolicyOwner(initial=SamplingPolicy(), fetch=SamplingPolicy)


@contextmanager
def held_installation[H: Installation | Loading](installation: H) -> Generator[H]:
    prior = slot()
    setattr(meiva_module(), SLOT, installation)
    try:
        yield installation
    finally:
        setattr(meiva_module(), SLOT, prior)
