from __future__ import annotations

from uuid import UUID

from base.clock.ids import new_uuid
from base.clock.tests.seeded_rng import seeded_rng


# meiva:note
# The identifier is a well-formed version 4 UUID, so a gateway or a database that parses the key
# accepts it, and its `hex` is the 32 lowercase hexadecimal characters the idempotency-key header
# carries.
def test_a_drawn_identifier_is_a_version_4_uuid() -> None:
    drawn = new_uuid(seeded_rng())

    assert drawn.version == 4
    assert drawn.variant == "specified in RFC 4122"
    assert UUID(drawn.hex) == drawn
    assert len(drawn.hex) == 32


# meiva:note
# The generator is the only source: the same seed answers the same identifiers in the same order,
# and each draw advances it, so consecutive identifiers differ.
def test_identifiers_follow_the_generator_and_never_repeat_within_it() -> None:
    first, second = seeded_rng(), seeded_rng()
    drawn = [new_uuid(first) for _ in range(3)]

    assert drawn == [new_uuid(second) for _ in range(3)]
    assert len(set(drawn)) == 3
