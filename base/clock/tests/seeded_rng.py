import random

__all__ = ["seeded_rng"]


# meiva:note
# The random generator a test hands to a component that draws jitter or a sampling decision:
# seeded, so a test that depends on the draw is repeatable. Neither is a secret, so the generator
# is a plain `random.Random`, like the one a process root builds.
def seeded_rng() -> random.Random:
    return random.Random(0)  # noqa: S311
