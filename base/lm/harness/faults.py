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
# meiva:link base/lm/harness/docs/harness.meiva.okf.md

from __future__ import annotations

from base.lm.harness.engine import HarnessTurn

__all__ = ["fault_message"]

# meiva:note
# The sentence a failed turn gets when the CLI named no cause. It says what was observed -- a
# failure was reported, no detail came with it -- and stops there: a "the result has no error
# field" reading would be an inference from a missing string, and it would be wrong for a field
# that exists and is empty, or for a turn that failed with no error detail at all.
NO_ERROR_DETAIL = "harness reported a failure but gave no error detail"


# meiva:note
# The kind prefers the HTTP status, because that is the field that names an API failure; a subtype
# that says "success" or "unknown" is not a kind at all.
def fault_message(harness: str, turn: HarnessTurn) -> str:
    kind = f"API {turn.api_error_status}" if turn.api_error_status is not None else turn.subtype
    if kind in {"success", "unknown"}:
        kind = "error"
    reported = f"{harness} turn {turn.turn_id} failed ({kind}): "
    detail = _declared_error(turn, kind)
    if detail:
        return reported + detail
    return reported + NO_ERROR_DETAIL + _known_status(turn)


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
