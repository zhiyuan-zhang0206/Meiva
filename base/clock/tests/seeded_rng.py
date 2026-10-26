import random

__all__ = ["seeded_rng"]


# meiva:note
# The random generator a test hands to a component that draws jitter or a sampling decision:
# seeded, so a test that depends on the draw is repeatable (seed 0 unless the test compares
# generators by seed). Neither is a secret, so the generator is a plain `random.Random`, like
# the one a process root builds.
def seeded_rng(seed: int = 0) -> random.Random:
    return random.Random(seed)  # noqa: S311
