# meiva:note
# OLD is the one commit every host agrees on that is not NEW. Hosts that disagree, or an OLD the
# operator's checkout does not carry, refuse the window. OLD only feeds the `.python-version`
# refusal: it decides nothing about what stops or starts.
# meiva:link cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class OldTarget:
    reasons: tuple[str, ...]
    resolved: str | None


# meiva:note
# The commit the hosts share besides NEW, or why they have none: more than one such commit means
# the fleet is not on a single OLD. A fleet already on NEW has no OLD, which is not a refusal.
def old_commit(heads: Mapping[str, str], new: str) -> OldTarget:
    older = {head for head in heads.values() if head != new}
    if len(older) > 1:
        return OldTarget((f"hosts disagree on HEAD: {dict(heads)}",), None)
    return OldTarget((), next(iter(older)) if older else None)


def unfetched_old_reason(old: str, repo: str) -> str:
    return f"OLD {old} is not in {repo}: git fetch origin"


# meiva:note
# A `.python-version` change rebuilds every venv, so it is the operator's call.
def python_change_reason(*, changed: bool, allowed: bool) -> tuple[str, ...]:
    if changed and not allowed:
        return (".python-version changes (venvs rebuild): pass --allow-python-change",)
    return ()
