# meiva:note
# What a failed turn says about itself. Two failures shaped this file. A Claude result that
# reports a failed API call carries `is_error=True` with `subtype="success"`, the status in
# `api_error_status` and the provider's sentence in `result` (#6150): read from the wrong fields
# the 400 printed as `failed (success): stop_sequence`, and the provider's own sentence was
# dropped. Then an `agy` turn that reported a failure and said nothing about the cause printed
# the assistant's own reply as that cause (#6498), which pointed three readers at three
# different wrong places.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md

from __future__ import annotations

from base.lm.harness.engine import HarnessTurn
from base.lm.harness.events import Usage
from base.lm.harness.faults import fault_message

# meiva:note
# The reply an `agy` turn streamed before it reported a failure: text the agent already read, and
# evidence about the failure to nobody.
REPLY = "MEIVA_HIDE_FROM_USER\n`wave-watch5` is still running; I will report when #3230 answers."


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
            stop_reason="stop_sequence",
            api_error_status=400,
            errors=("API Error: 400 This API key is not scoped to a workspace",),
        ),
    )

    assert message == (
        "claude_code turn turn-1 failed (API 400): "
        "API Error: 400 This API key is not scoped to a workspace"
    )


# meiva:note
# The failure this file exists for after #6498: the turn failed, the CLI said nothing about why,
# and the only text on the turn is the reply the assistant had already written. Saying so beats
# quoting the reply, and the status the CLI does report is kept beside the admission.
def test_the_assistant_reply_is_never_the_failure_detail() -> None:
    message = fault_message("antigravity", turn(text=REPLY, subtype="error", stop_reason="ERROR"))

    assert message == (
        "antigravity turn turn-1 failed (error): harness reported a failure but gave no error"
        " detail (status/stop_reason: ERROR)"
    )
    assert REPLY not in message
    assert "still running" not in message


def test_a_failure_that_reports_nothing_else_still_says_it_has_no_detail() -> None:
    message = fault_message("claude_code", turn(text=REPLY))

    assert message == (
        "claude_code turn turn-1 failed (error): "
        "harness reported a failure but gave no error detail"
    )


# meiva:note
# A stop reason is not a statement about the failure, so it is named as the status it is instead
# of being quoted as the reason -- "usage limit reached" reads the same either way, but
# `stop_sequence` on a dead API call used to read as if it were the cause.
def test_a_stop_reason_is_named_as_the_status_not_as_the_reason() -> None:
    limited = fault_message(
        "claude_code", turn(subtype="failed", stop_reason="usage limit reached")
    )
    status_only = fault_message(
        "claude_code", turn(api_error_status=400, stop_reason="stop_sequence")
    )

    assert limited == (
        "claude_code turn turn-1 failed (failed): harness reported a failure but gave no error"
        " detail (status/stop_reason: usage limit reached)"
    )
    assert status_only == (
        "claude_code turn turn-1 failed (API 400): harness reported a failure but gave no error"
        " detail (status/stop_reason: stop_sequence)"
    )


# meiva:note
# A subtype that says "success" cannot be the failure kind even when the CLI reports no status: the
# turn carries `is_error`, and that is what a reader has to see.
def test_a_success_subtype_is_never_the_failure_kind() -> None:
    message = fault_message("claude_code", turn(text="the connection dropped"))

    assert message == (
        "claude_code turn turn-1 failed (error): "
        "harness reported a failure but gave no error detail"
    )
    assert "the connection dropped" not in message


def test_the_cli_error_list_wins_over_the_result_text() -> None:
    message = fault_message(
        "claude_code", turn(text="the result text", errors=("context window exceeded",))
    )

    assert message == "claude_code turn turn-1 failed (error): context window exceeded"


def test_antigravity_fault_message_prefers_cli_error_over_hidden_text() -> None:
    message = fault_message(
        "antigravity",
        turn(
            text="MEIVA_HIDE_FROM_USER\nhidden assistant text",
            errors=("API error (attempt 1): 500 Internal Server Error",),
        ),
    )

    assert message == (
        "antigravity turn turn-1 failed (error): API error (attempt 1): 500 Internal Server Error"
    )
