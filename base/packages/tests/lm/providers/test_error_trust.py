from typing import ClassVar

import httpx
import httpx2
import openai
import pytest
from langchain_anthropic.chat_models import AnthropicConnectionError, AnthropicTimeoutError
from langchain_core.exceptions import (
    ContextOverflowError,
    ModelAuthenticationError,
    ModelError,
    ModelRateLimitError,
)
from langchain_openai.chat_models.base import OpenAIConnectionError, OpenAITimeoutError

from base.lm.errors import ErrorClass, classify_error


@pytest.mark.parametrize(
    "error_type",
    [AnthropicConnectionError, AnthropicTimeoutError, OpenAIConnectionError, OpenAITimeoutError],
)
def test_official_connection_wrappers_are_transient(
    error_type: type[
        AnthropicConnectionError
        | AnthropicTimeoutError
        | OpenAIConnectionError
        | OpenAITimeoutError
    ],
) -> None:
    request = httpx2.Request("POST", "https://audit.invalid")
    error = error_type(request=request)
    assert isinstance(error, ModelError) and error.is_retryable
    assert classify_error(error).error_class is ErrorClass.TRANSIENT


@pytest.mark.parametrize("status", [400, 429])
def test_programming_error_cannot_borrow_provider_cause(status: int) -> None:
    response = httpx2.Response(status, request=httpx2.Request("POST", "https://audit.invalid"))
    provider = openai.APIStatusError("provider failure", response=response, body=None)
    error = TypeError("new programming error")
    error.__cause__ = provider
    classified = classify_error(error)
    assert classified.error_class is ErrorClass.UNKNOWN
    assert classified.status is None
    assert not classified.billing and not classified.context_overflow


def test_matching_attributes_do_not_grant_provider_authority() -> None:
    class UnrelatedError(Exception):
        status_code = 429
        is_retryable = True
        body: ClassVar[dict[str, dict[str, str]]] = {"error": {"type": "engine_overloaded_error"}}

    classified = classify_error(UnrelatedError())
    assert classified.error_class is ErrorClass.UNKNOWN
    assert classified.status is None
    assert classified.error_type is None


@pytest.mark.parametrize(
    "error",
    [
        ConnectionError("other service"),
        TimeoutError("other operation"),
        httpx.ConnectError("other HTTP"),
    ],
)
def test_raw_transport_is_not_provider_authority_outside_call_boundary(error: Exception) -> None:
    assert classify_error(error).error_class is ErrorClass.UNKNOWN


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ModelRateLimitError("limited"), ErrorClass.TRANSIENT),
        (ModelAuthenticationError("credentials"), ErrorClass.PERMANENT),
        (ModelError("unclassified model failure"), ErrorClass.UNKNOWN),
    ],
)
def test_official_model_taxonomy_is_authoritative(error: ModelError, expected: ErrorClass) -> None:
    assert classify_error(error).error_class is expected


def test_typed_context_overflow_keeps_rescue_authority_without_guessing_http_status() -> None:
    classified = classify_error(ContextOverflowError("context limit"))
    assert classified.error_class is ErrorClass.PERMANENT
    assert classified.context_overflow
    assert classified.status is None


@pytest.mark.parametrize("error_type", ["service_unavailable_error", "server_error"])
@pytest.mark.parametrize("nested", [False, True])
def test_openai_stream_service_failure_retries_without_inventing_http_status(
    error_type: str, nested: bool
) -> None:
    body = {"type": error_type, "message": "Our servers are currently overloaded."}
    error = openai.APIError(
        body["message"],
        request=httpx2.Request("POST", "https://audit.invalid"),
        body={"error": body} if nested else body,
    )
    classified = classify_error(error)
    assert classified.error_class is ErrorClass.TRANSIENT
    assert classified.status is None
    assert classified.error_type == error_type
    assert not classified.billing


@pytest.mark.parametrize("error_type", ["unknown", "invalid_request_error", "insufficient_quota"])
def test_openai_stream_message_alone_does_not_grant_retry(error_type: str) -> None:
    error = openai.APIError(
        "Our servers are currently overloaded. Please try again later.",
        request=httpx2.Request("POST", "https://audit.invalid"),
        body={"type": error_type},
    )
    assert classify_error(error).error_class is ErrorClass.UNKNOWN


def test_openai_stream_service_type_does_not_override_permanent_http_status() -> None:
    error = openai.BadRequestError(
        "bad request",
        response=httpx2.Response(400, request=httpx2.Request("POST", "https://audit.invalid")),
        body={"type": "service_unavailable_error"},
    )
    assert classify_error(error).error_class is ErrorClass.PERMANENT


def test_unrelated_wrapper_cannot_borrow_openai_stream_service_failure() -> None:
    error = RuntimeError("application bug")
    error.__cause__ = openai.APIError(
        "overloaded",
        request=httpx2.Request("POST", "https://audit.invalid"),
        body={"type": "service_unavailable_error"},
    )
    assert classify_error(error).error_class is ErrorClass.UNKNOWN
