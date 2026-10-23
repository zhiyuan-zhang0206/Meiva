# meiva:note
# Whether a stop may start at all: who is running it, and whether the operator agreed.

from __future__ import annotations

__all__ = ["CONFIRM_PROMPT", "confirmed", "hosted_stop_refusal"]

CONFIRM_PROMPT = "confirm? [y/N] "


# meiva:note
# The stdin "y/N" gate: only an explicit `y` lets the stop proceed (EOF reads as no).
def confirmed(answer: str) -> bool:
    return answer.strip().lower() == "y"


# meiva:note
# Why a stop must not run here, or None when it may.
#
# A stop run from inside the work it drains strands itself mid-drain. `exec_domain` is the
# execute_code exec domain this process runs in; `supervised` is the supervised session that
# hosts it.
def hosted_stop_refusal(exec_domain: str | None, supervised: str | None) -> str | None:
    if exec_domain is not None:
        # meiva:note
        # An exec-domain leg is SIGKILLed with the call's process group as the tool call
        # returns, mid-drain (the 2026-09-12 stranding shape). A stop closes this unit's
        # persistent shell sessions too, so no shell session can host it either.
        return (
            "stop cannot run inside execute_code: the call's teardown SIGKILLs its "
            "process group as the call returns, stranding the stop mid-drain; a stop "
            "closes this unit's persistent shell sessions too — run it from a shell no meiva "
            "session hosts (e.g. a plain login shell)"
        )
    if supervised is not None:
        return "stop must run outside the work it drains; use a login shell"
    return None
