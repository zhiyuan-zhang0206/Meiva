from __future__ import annotations

from base.lm.harness.codex.frames import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    RequestParams,
    as_object,
    as_text,
)


# meiva:note
# A `turn/started` this session did not ask for names a turn the server runs on its own. Only one
# that says it is in progress is a live turn: a transcript the server replays on resume can carry
# the same notification for a turn that ended long ago, and adopting it would hold the session busy
# for a completion that never comes.
# meiva:link base/lm/harness/docs/delivery.md
def observed_turn_name(params: RequestParams) -> str | None:
    turn = as_object(params.get("turn"))
    return as_text(turn.get("id")) if turn.get("status") == "inProgress" else None


# meiva:note
# Which refusals of a `turn/steer` or `turn/start` are a verdict on the input rather than a fault of
# the server: the protocol says the turn is not there to steer (`-32600`, "no active turn to
# steer"), has no such method, or did not accept the params. Each comes back as the reason an
# `InputRejected` carries, so the caller can put the input back in its queue. Any other code is the
# server failing and stays an error: nothing says the input was not taken.
# meiva:link base/lm/harness/codex/docs/codex.md
def rejection_reason(code: int, message: str) -> str | None:
    if code in (INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS):
        return f"{code}: {message}"
    return None
