from __future__ import annotations

import random
from uuid import UUID

__all__ = ["new_uuid"]


# meiva:note
# A random (version 4) UUID drawn from `rng`: the one way a component above `base` makes an
# identifier, an idempotency key or a record name. It reads no entropy of its own, so the process
# root that built `rng` is the only place that chooses the source (a `random.SystemRandom` where
# the id must be unguessable) and a test that hands in a seeded generator gets the same ids on
# every run.
def new_uuid(rng: random.Random) -> UUID:
    return UUID(int=rng.getrandbits(128), version=4)
