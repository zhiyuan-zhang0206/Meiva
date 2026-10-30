from __future__ import annotations

import asyncio
import json
import os
import random
import sys
import urllib.error

import click

from base.host.net.resilience import (
    ExponentialBackoff,
    Policy,
    agent_phase,
    aretry,
    jittered,
    retry,
)


def transports() -> dict[str, object]:
    import base.host.net

    assert base.host.net.__name__ == "base.host.net"
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"path": request.url.path})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert client.get("http://x.test/sync").json() == {"path": "/sync"}

    async def async_get() -> object:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return (await client.get("http://x.test/async")).json()

    assert asyncio.run(async_get()) == {"path": "/async"}
    return {"transport_ok": True}


def cli_help() -> dict[str, object]:
    import base.host.net

    assert base.host.net.__name__ == "base.host.net"
    import httpx

    assert isinstance(httpx.main, click.Command)
    try:
        httpx.main(args=["--help"], prog_name="httpx")
    except SystemExit as error:
        return {"exit_code": error.code}
    raise AssertionError("HTTPX help did not exit")


# meiva:note
# A fixed child-only seed verifies non-secret retry spreading without mutating the test runner.
def retry_jitter(mode: str) -> dict[str, object]:
    seed = 1742
    random.seed(seed)
    expected_noise = random.Random(seed).uniform(-0.5, 0.5)  # noqa: S311
    attempts = 0

    def flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise urllib.error.URLError("retry me")
        return "ok"

    span = 5.0 if mode == "phase" else 0.5
    waits: list[float] = []
    result = retry(Policy(max_attempts=2, jitter=mode, jitter_span=span), sleep=waits.append)(flaky)
    return {
        "result": result,
        "waits": waits,
        "phase": agent_phase(span),
        "noise": expected_noise,
    }


def async_waits() -> dict[str, object]:
    attempts = 0
    failure = urllib.error.URLError("still offline")

    async def flaky() -> None:
        nonlocal attempts
        attempts += 1
        raise failure

    waits: list[float] = []

    async def record(seconds: float) -> None:
        waits.append(seconds)

    run = aretry(
        Policy(max_attempts=3, backoff=ExponentialBackoff(1, 2, 8), jitter="none"),
        asleep=record,
    )

    async def drive() -> None:
        await run(flaky)

    try:
        asyncio.run(drive())
    except urllib.error.URLError as error:
        same_error = error is failure
    else:
        raise AssertionError("retry exhaustion did not propagate")
    return {"attempts": attempts, "waits": waits, "same_error": same_error}


def main(case: str) -> dict[str, object]:
    if case == "transports":
        return transports()
    if case == "cli-help":
        return cli_help()
    if case in {"phase", "relative"}:
        return retry_jitter(case)
    if case == "async-waits":
        return async_waits()
    if case == "identity":
        return {"pid": os.getpid(), "phase": jittered(0, span=10, mode="phase")}
    raise ValueError(f"unknown resilience test case: {case!r}")


if __name__ == "__main__":
    sys.stdout.write(json.dumps(main(sys.argv[1])))
