# meiva:note
# What a failed harness turn says about itself, decided apart from the bridge that raises it.
#
# A failure has one detail: what the CLI declared as an error. Everything else the CLI reports
# about the turn -- its stop reason, its status -- is a fact about how the turn ended, not a
# statement about what went wrong, so it is named as a fact and never quoted as the reason.
#
# Two incidents decided that split. A Claude result that reports a failed API call carries
# `is_error=True` with `subtype="success"` and the HTTP status in `api_error_status` -- the SDK
# documents that shape; read from the wrong fields, the 400 that ended a claude_code turn on
# 2026-11-04 printed `failed (success): stop_sequence`, naming a successful stop reason and a
# successful subtype for a dead turn, while the provider's own sentence was dropped. Then on
# 2026-11-07 an `agy` turn that reported a failure without saying why printed the assistant's own
# reply as the reason, and that drove three readers into three different wrong places -- an output
# prefix, a CLI schema, a tool call. The reply is text the agent already read; it is evidence
# about the failure to nobody.
#
# meiva:link base/lm/harness/docs/harness.md

from __future__ import annotations

from base.lm.harness.engine import HarnessTurn
from base.lm.harness.events import TurnCompleted

__all__ = ["fault_message", "requires_session_recycle", "session_recycle_reason"]

# meiva:note
# The sentence a failed turn gets when the CLI named no cause. It says what was observed -- a
# failure was reported, no detail came with it -- and stops there: a "the result has no error
# field" reading would be an inference from a missing string, and it would be wrong for a field
# that exists and is empty, or for a turn that failed with no error detail at all.
NO_ERROR_DETAIL = "harness reported a failure but gave no error detail"

_AUTH_RECYCLE_PHRASES = (
    "authentication_failed",
    "failed to authenticate",
    "login reached its time limit",
    "claude auth login",
    "oauth token has expired",
    "oauth token expired",
)


# meiva:note
# The kind prefers the HTTP status, because that is the field that names an API failure; a subtype
# that says "success" or "unknown" is not a kind at all, so a synthetic assistant `error_code`
# (such as `authentication_failed`) takes over there rather than collapsing to generic `error`.
def fault_message(harness: str, turn: HarnessTurn) -> str:
    if turn.api_error_status is not None:
        kind = f"API {turn.api_error_status}"
    elif turn.subtype not in {"success", "unknown"}:
        kind = turn.subtype
    elif turn.error_code:
        kind = turn.error_code
    else:
        kind = "error"
    reported = f"{harness} turn {turn.turn_id} failed ({kind}): "
    detail = _declared_error(turn, kind)
    if detail:
        if (
            turn.error_code
            and turn.error_code.lower() not in kind.lower()
            and turn.error_code.lower() not in detail.lower()
        ):
            detail = f"{turn.error_code}: {detail}"
        return reported + detail
    if turn.error_code and turn.error_code.lower() not in kind.lower():
        return reported + turn.error_code + _known_status(turn)
    return reported + NO_ERROR_DETAIL + _known_status(turn)


# meiva:note
# A long-lived CLI subprocess caches OAuth credentials in memory: when the host refreshes Keychain
# credentials externally, that subprocess keeps rejecting turns with `authentication_failed` until
# recycled. Only declared error fields (never assistant reply prose) can trigger retirement so a
# turn that merely discusses auth errors is never mistaken for a broken session.
def session_recycle_reason(turn: HarnessTurn | TurnCompleted) -> str | None:
    if not turn.is_error or turn.interrupted:
        return None
    if turn.error_code is not None and turn.error_code.strip().lower() == "authentication_failed":
        return "session authentication failed"
    if turn.api_error_status == 401:
        return "session authentication failed"
    for candidate in turn.errors:
        lowered = candidate.lower()
        if any(phrase in lowered for phrase in _AUTH_RECYCLE_PHRASES):
            return "session authentication failed"
    return None


def requires_session_recycle(turn: HarnessTurn | TurnCompleted) -> bool:
    return session_recycle_reason(turn) is not None


# meiva:note
# A candidate that only repeats the kind (`ERROR` for an `error` turn) says nothing new, so it is
# not a detail either; the status is still reported beside the admission below.
def _declared_error(turn: HarnessTurn, kind: str) -> str:
    uninformative = {kind.lower(), turn.subtype.lower()}
    for candidate in turn.errors:
        text = candidate.strip()
        if text and text.lower() not in uninformative:
            return text
    return ""


# meiva:note
# The fields a turn reports about how it ended, kept beside a missing detail so a reader still
# sees the shape the CLI gave: `agy` puts its result status here, and it is where some CLIs put a
# non-API failure such as "usage limit reached".
def _known_status(turn: HarnessTurn) -> str:
    status = (turn.stop_reason or "").strip()
    return f" (status/stop_reason: {status})" if status else ""
