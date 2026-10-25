# meiva:note
# When a keepalive round counts as a failure, when the probe is merely blind, and when
# either is worth a wake.
#
# The keeper probes a page and a remote process every ten minutes. One bad round is a
# restart, a rollout or a five-second network hiccup; two in a row is a host that is not
# coming back on its own. A probe that cannot answer is neither healthy nor broken: it
# carries its own streak and, after three rounds, its own "cannot probe" wake, because
# folding `unknown` into "healthy" is how a keeper silently stops keeping anything. Each
# wake holds an hour of cool-down, and only a determinate answer clears the blind streak.
# meiva:link schedules/README.md

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

__all__ = ["KeepaliveState", "RoundContext", "RoundPlan", "plan_round"]

WAKE_AFTER_FAILS = 2
BLIND_AFTER_ROUNDS = 3
COOLDOWN_SECONDS = 3600.0


# meiva:note
# What the keeper carries between rounds: the outage streak and its first stamp, the blind
# streak and its first stamp, and when each kind of wake last fired.
@dataclass(frozen=True)
class KeepaliveState:
    fails: int = 0
    first_fail_at: str | None = None
    blind: int = 0
    blind_since: str | None = None
    last_wake_at: float = 0.0
    last_blind_wake_at: float = 0.0


# meiva:note
# What a round says about where it ran and how to recover: the ISO stamp the state keeps and the
# three texts a wake carries. Grouped so the rule's signature stays about the decision.
@dataclass(frozen=True)
class RoundContext:
    stamp: str
    host: str
    hint: str
    blind_hint: str


# meiva:note
# `wake` is the message to send the owner, or None. The state is always the one to save, so
# a healthy round writes the cleared streaks together with its proof-of-life stamp.
@dataclass(frozen=True)
class RoundPlan:
    state: KeepaliveState
    wake: str | None


# meiva:note
# A stamp of zero means "this keeper has never woken anyone", which is not the same as "the
# cool-down has not elapsed": reading the two as one makes the first wake depend on how large the
# clock happens to be.
def _elapsed(last: float, now: float) -> bool:
    return last == 0.0 or now - last > COOLDOWN_SECONDS


def plan_round(
    state: KeepaliveState,
    problems: Sequence[str],
    *,
    blind: bool,
    now: float,
    context: RoundContext,
) -> RoundPlan:
    fails = state.fails + 1 if problems else 0
    first_fail_at = (state.first_fail_at or context.stamp) if problems else None
    blind_rounds = state.blind + 1 if blind else 0
    blind_since = (state.blind_since or context.stamp) if blind else None
    last_wake_at = state.last_wake_at
    last_blind_wake_at = state.last_blind_wake_at
    wake = None
    if problems and fails >= WAKE_AFTER_FAILS and _elapsed(last_wake_at, now):
        last_wake_at = now
        wake = "[qa-keepalive] %s: %d consecutive failed rounds (first at %s):\n- %s\n%s" % (
            context.host,
            fails,
            first_fail_at,
            "\n- ".join(problems),
            context.hint,
        )
    elif blind and blind_rounds >= BLIND_AFTER_ROUNDS and _elapsed(last_blind_wake_at, now):
        last_blind_wake_at = now
        wake = "[qa-keepalive] %d consecutive rounds could not probe %s (first at %s). %s" % (
            blind_rounds,
            context.host,
            blind_since,
            context.blind_hint,
        )
    return RoundPlan(
        KeepaliveState(
            fails=fails,
            first_fail_at=first_fail_at,
            blind=blind_rounds,
            blind_since=blind_since,
            last_wake_at=last_wake_at,
            last_blind_wake_at=last_blind_wake_at,
        ),
        wake,
    )
