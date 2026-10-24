# meiva:note
# What a lease's control wake may claim. An external lease owns decisions, so the claim that
# runs under it is limited to lifecycle commands; anything else in the batch means the claim
# stepped outside its contract and the wake must stop instead of acting on it.

from collections.abc import Sequence


# meiva:note
# `durable` holds, for each claimed item, whether it is a durable lifecycle command. At most
# one command is accepted per wake, and it must be one.
def require_lifecycle_claim(durable: Sequence[bool]) -> None:
    if len(durable) > 1 or not all(durable):
        raise RuntimeError("lease control claim returned an unaccepted command")
