# meiva:note
# What a failed turn says about itself. The failure this was written for (#6150) is a Claude result
# that reports a failed API call as `is_error=True` with `subtype="success"` and the status in
# `api_error_status`: read from the wrong fields the 400 printed as
# `failed (success): stop_sequence`, and the provider's own sentence was dropped.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md

from __future__ import annotations

from base.lm.harness.engine import HarnessTurn
from base.lm.harness.events import Usage
from base.lm.harness.faults import fault_message


def turn(
    *,
    text: str = "",
    subtype: str = "success",
    stop_reason: str | None = None,
    api_error_status: int | None = None,
    errors: tuple[str, ...] = (),
) -> HarnessTurn:
    return HarnessTurn(
        turn_id="turn-1",
        text=text,
        usage=Usage(input_tokens=1, output_tokens=1),
        cost_usd=None,
        subtype=subtype,
        is_error=True,
        interrupted=False,
        stop_reason=stop_reason,
        api_error_status=api_error_status,
        errors=errors,
    )


def test_a_failed_api_call_is_named_by_its_status_and_the_provider_text() -> None:
    message = fault_message(
        "claude_code",
        turn(
            text="API Error: 400 This API key is not scoped to a workspace",
            stop_reason="stop_sequence",
            api_error_status=400,
        ),
    )

    assert message == (
        "claude_code turn turn-1 failed (API 400): "
        "API Error: 400 This API key is not scoped to a workspace"
    )


# meiva:note
# A subtype that says "success" cannot be the failure kind even when the CLI reports no status: the
# turn carries `is_error`, and that is what a reader has to see.
def test_a_success_subtype_is_never_the_failure_kind() -> None:
    message = fault_message("claude_code", turn(text="the connection dropped"))

    assert message == "claude_code turn turn-1 failed (error): the connection dropped"


def test_the_cli_error_list_wins_over_the_result_text() -> None:
    message = fault_message(
        "claude_code", turn(text="the result text", errors=("context window exceeded",))
    )

    assert message == "claude_code turn turn-1 failed (error): context window exceeded"


# meiva:note
# `stop_reason` is a *stop* reason, so it is read only when nothing else names the failure -- which
# is where a CLI puts "usage limit reached". Reading it first is what turned a 400 into
# `stop_sequence`.
def test_a_stop_reason_is_read_only_as_the_last_resort() -> None:
    limited = fault_message(
        "claude_code", turn(subtype="failed", stop_reason="usage limit reached")
    )
    overshadowed = fault_message(
        "claude_code",
        turn(text="API Error: 429 slow down", stop_reason="usage limit reached"),
    )

    assert limited == "claude_code turn turn-1 failed (failed): usage limit reached"
    assert overshadowed == "claude_code turn turn-1 failed (error): API Error: 429 slow down"
