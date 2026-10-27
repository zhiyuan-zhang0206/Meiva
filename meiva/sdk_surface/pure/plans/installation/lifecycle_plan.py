# meiva:note
# What a plugin load and its rollback settle on, decided from values their callers collected: the
# sampling owner, the clients and the clock a full load keeps from the installation it replaces
# (and the refusal of a first load with no owner to retain), and the failure a rollback ends with.
# The loader (`extensions.py`) and the installer (`install.py`) read the process and run the
# effects; these functions only choose.
# meiva:link meiva/docs/sdk-surface/install-lifetime.meiva.okf.md

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from base.clock import Clock
from meiva.sdk_surface.installation import EventPipelines, Installation, SamplingPolicyOwner


@dataclass(frozen=True, slots=True)
class Carried:
    sampling: SamplingPolicyOwner
    clients: EventPipelines | None
    clock: Clock | None


# meiva:note
# The owner, clients and clock one load runs with: each the caller named, else the replaced
# installation's (`previous`, absent for a surface-only load and for a first load). A load with
# no owner after that is refused, because the installation it builds must retain one.
def carry_over(
    previous: Installation | None,
    *,
    sampling: SamplingPolicyOwner | None,
    clients: EventPipelines | None,
    clock: Clock | None,
) -> Carried:
    if previous is not None:
        if sampling is None:
            sampling = previous.sampling
        if clients is None:
            clients = previous.clients
        if clock is None:
            clock = previous.clock
    if sampling is None:
        raise ValueError("the first plugin load needs the sampling owner its root retains")
    return Carried(sampling=sampling, clients=clients, clock=clock)


def _also_failed(exc: BaseException) -> str:
    return f"SDK rollback also failed: {type(exc).__name__}: {exc}"


# meiva:note
# The exception a rollback ends with, given the failure that triggered it (`primary`, None when
# the rollback was asked for on its own) and the failures its reversals met, in the order they ran.
# Every reversal was attempted. With a primary failure it stays primary and carries the cleanup
# failures as notes, and nothing more is raised (None: the caller re-raises the primary). With none,
# the first cleanup failure is raised and carries the later ones as notes; no failure at all is
# success.
def rollback_outcome(
    primary: BaseException | None, cleanup_failures: Sequence[BaseException]
) -> BaseException | None:
    if primary is not None:
        for exc in cleanup_failures:
            primary.add_note(_also_failed(exc))
        return None
    if not cleanup_failures:
        return None
    first, *later = cleanup_failures
    for exc in later:
        first.add_note(_also_failed(exc))
    return first
