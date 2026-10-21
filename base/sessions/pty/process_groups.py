"""Best-effort signals for known PTY process groups, with birth checks.

No process enumeration or descendant discovery occurs here. A surviving known
identity can be reported; an empty result says nothing about untracked jobs.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable

from base.native_process.ownership import OwnedProcess

_REFUSED_SETTLE_S = 0.25


def live(identity: OwnedProcess) -> bool:
    """Whether a known birth still lives; unverifiable identities raise."""
    return identity.live()


def signal(
    identities: Iterable[OwnedProcess],
    signum: int,
    *,
    kill_group: Callable[[int, int], None] | None = None,
) -> None:
    """Signal each known group once, while its recorded member still verifies.

    The caller's own group is never signaled as a batch: only the recorded birth
    is signaled. Process disappearance is normal; other OS errors propagate.
    """
    sent: set[int] = set()
    own_group = os.getpgrp()
    kill = os.killpg if kill_group is None else kill_group
    for identity in identities:
        if not live(identity):
            continue
        if identity.pid == os.getpid():
            raise RuntimeError("PTY cleanup cannot signal its own process")
        try:
            group = os.getpgid(identity.pid)
            if not live(identity):
                continue
            if group == own_group:
                identity.send_signal(signum)
            elif group not in sent:
                kill(group, signum)
                sent.add(group)
        except ProcessLookupError:
            continue
        except PermissionError as exc:
            # meiva:note
            # A group signal is refused while its leader is exiting: measured 2026-10-21, the
            # pid read `idle` at the refusal and was gone microseconds later, so the OS answered
            # as if the group were not ours. That is the disappearance this module already
            # treats as normal — a birth that outlives the short wait is the loud one, and only
            # then is the refusal a permission problem worth raising.
            if not wait((identity,), _REFUSED_SETTLE_S):
                continue
            raise PermissionError(
                f"PTY signal {signum} denied for group of known PID {identity.pid}: {exc}"
            ) from exc


def wait(identities: Iterable[OwnedProcess], timeout_s: float) -> tuple[OwnedProcess, ...]:
    """Poll only known identities until they exit or the bounded wait ends."""
    known = tuple(dict.fromkeys(identities))
    deadline = time.monotonic() + timeout_s
    while True:
        remaining = tuple(identity for identity in known if live(identity))
        left = deadline - time.monotonic()
        if not remaining or left <= 0:
            return remaining
        time.sleep(min(0.02, left))
