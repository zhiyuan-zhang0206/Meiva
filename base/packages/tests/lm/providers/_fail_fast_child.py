# meiva:note
# Observe real sleep calls in this process while retaining nonzero production retry delays.
# meiva:link base/packages/tests/lm/docs/contracts.meiva.okf.md

import sys
import time
from types import FrameType
from typing import Any

import httpx2
import openai
import pytest

from base.lm.call import invoke_response
from base.packages.plugin_providers import build_model_catalog


def main(error: Exception, provider_cause: bool) -> None:
    model_catalog = build_model_catalog()
    calls: list[list[Any]] = []
    sleeps: list[object] = []
    if provider_cause:
        response = httpx2.Response(429, request=httpx2.Request("POST", "https://audit.invalid"))
        error.__cause__ = openai.RateLimitError("limited", response=response, body=None)

    class BrokenRunnable:
        def invoke(self, messages: list[Any]) -> Any:
            calls.append(messages)
            raise error

    forbid_sleep = False

    def observe(frame: FrameType, event: str, arg: object) -> None:
        if event == "c_call" and arg is time.sleep:
            sleeps.append(arg)
            if forbid_sleep:
                raise AssertionError("unknown runnable errors must not request a sleep")

    previous = sys.getprofile()
    sys.setprofile(observe)
    try:
        time.sleep(0)
        assert sleeps == [time.sleep]
        sleeps.clear()
        forbid_sleep = True
        with pytest.raises(type(error)) as raised:
            invoke_response(
                BrokenRunnable(),
                [],
                desc="audit",
                error_type=RuntimeError,
                retry_attempts=2,
                catalog=model_catalog,
            )
        assert raised.value is error
        assert len(calls) == 1 and sleeps == []
    finally:
        sys.setprofile(previous)


if __name__ == "__main__":
    error_kind, provider_cause = sys.argv[1:]
    match error_kind:
        case "TypeError":
            error = TypeError("bad code")
        case "ValueError":
            error = ValueError("bad input")
        case _:
            raise ValueError(f"Unknown fail-fast scenario: {error_kind!r}")
    if provider_cause not in ("False", "True"):
        raise ValueError(f"Unknown provider-cause flag: {provider_cause!r}")
    main(error, provider_cause == "True")
