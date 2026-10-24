# meiva:note
# Put an `Installation` value into the process slot for a `with` block and restore the previous
# holder afterwards. The slot owner's own test support: a test of a facade that reads the
# installation (the model catalog, the configuration authority, the clock) needs the process
# root to exist, and holds the value it built instead of replacing the slot's name.
# `install.install` is the real writer of a full surface; this holds a bare value only.
from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from meiva.sdk_surface import meiva_module
from meiva.sdk_surface.installation import SLOT, Installation, slot

__all__ = ["held_installation"]


@contextmanager
def held_installation(installation: Installation) -> Generator[Installation]:
    prior = slot()
    setattr(meiva_module(), SLOT, installation)
    try:
        yield installation
    finally:
        setattr(meiva_module(), SLOT, prior)
