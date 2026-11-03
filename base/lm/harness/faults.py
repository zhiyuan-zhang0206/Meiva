# meiva:note
# What a failed harness turn says about itself, decided apart from the bridge that raises it.
#
# A Claude result that reports a failed API call carries `is_error=True` with `subtype="success"`
# and the HTTP status in `api_error_status` -- the SDK documents that shape. Read from the wrong
# fields, the 400 that ended a claude_code turn on 2026-11-04 printed as
# `failed (success): stop_sequence`, naming a successful stop reason and a successful subtype for a
# dead turn, while the provider's own sentence (`API Error: 400 This API key is not scoped to a
# workspace`) was dropped.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md

from __future__ import annotations

from base.lm.harness.engine import HarnessTurn

__all__ = ["fault_message"]


# meiva:note
# The kind prefers the HTTP status, because that is the field that names an API failure; a subtype
# that says "success" or "unknown" is not a kind at all.
#
# The detail prefers what the provider wrote -- its own error list, then the result text -- and
# falls back to `stop_reason` last: that field is a *stop* reason rather than an error, yet it is
# the only place some CLIs put a non-API failure such as "usage limit reached".
def fault_message(harness: str, turn: HarnessTurn) -> str:
    kind = f"API {turn.api_error_status}" if turn.api_error_status is not None else turn.subtype
    if kind in {"success", "unknown"}:
        kind = "error"
    detail = next(
        (
            candidate.strip()
            for candidate in (*turn.errors, turn.text, turn.stop_reason)
            if candidate and candidate.strip() and candidate.strip() != turn.subtype
        ),
        "",
    )
    suffix = f": {detail}" if detail else ""
    return f"{harness} turn {turn.turn_id} failed ({kind}){suffix}"
