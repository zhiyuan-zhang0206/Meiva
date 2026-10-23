from typing import Any

import httpx2
import openai
import pytest

from base.lm.call import invoke_response
from base.lm.catalog import ModelCatalog


@pytest.mark.parametrize("error", [TypeError("bad code"), ValueError("bad input")])
@pytest.mark.parametrize("provider_cause", [False, True])
def test_unknown_runnable_error_is_not_retried_or_wrapped(
    model_catalog: ModelCatalog,
    error: Exception,
    provider_cause: bool,
) -> None:
    calls: list[list[Any]] = []
    if provider_cause:
        response = httpx2.Response(429, request=httpx2.Request("POST", "https://audit.invalid"))
        error.__cause__ = openai.RateLimitError("limited", response=response, body=None)

    class BrokenRunnable:
        def invoke(self, messages: list[Any]) -> Any:
            calls.append(messages)
            raise error

    with pytest.raises(type(error)) as raised:
        invoke_response(
            BrokenRunnable(),
            [],
            desc="audit",
            error_type=RuntimeError,
            retry_attempts=2,
            retry_delay_seconds=0.0,
            retry_max_delay_seconds=0.0,
            catalog=model_catalog,
        )
    assert raised.value is error
    assert len(calls) == 1
