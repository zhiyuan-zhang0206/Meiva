"""Cold-interpreter SDK telemetry scenarios; never imported by a production entry.

Run as ``python -I _sdk_cold_child.py <scenario>``. ``capture_import_failure`` reads the injected
error class from ``TEST_CAPTURE_ERROR``.
"""

from __future__ import annotations

import importlib.abc
import os
import sys
from pathlib import Path


def cold_import_loads_no_httpx() -> None:
    import base.agents.sdk.telemetry  # noqa: F401  # pyright: ignore[reportUnusedImport]

    assert "httpx" not in sys.modules
    assert "httpcore" not in sys.modules


def capture_import_failure() -> None:
    import asyncio

    from base.agents.sdk import call_policy, telemetry
    from base.agents.sdk.call_policy import SamplingPolicy
    from base.agents.sdk.tally import SdkCallTally

    assert "base.agents.impersonation.manifest" not in sys.modules
    error_type = {"ImportError": ImportError, "RuntimeError": RuntimeError}[
        os.environ["TEST_CAPTURE_ERROR"]
    ]
    async_call = os.environ.get("TEST_CAPTURE_ASYNC") == "1"
    failure = error_type("invalid cold capture module")
    executed: list[str] = []

    class BrokenCapture(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname: str, path: object, target: object = None) -> None:
            if fullname == "base.agents.impersonation.manifest":
                raise failure

    async def async_body() -> None:
        executed.append("body")

    tally = SdkCallTally()
    owner = call_policy.SamplingPolicyOwner(
        initial=SamplingPolicy(),
        source=call_policy.SamplingSource(env={}, env_path=Path("absent.env")),
    )
    sys.meta_path.insert(0, BrokenCapture())
    try:
        if async_call:
            asyncio.run(
                telemetry.run_metered_async(
                    "capture.call",
                    async_body,
                    (),
                    {},
                    identity={},
                    tally=tally,
                    sampling_owner=owner,
                )
            )
        else:
            telemetry.run_metered(
                "capture.call",
                lambda: executed.append("body"),
                (),
                {},
                identity={},
                tally=tally,
                sampling_owner=owner,
            )
    except (ImportError, RuntimeError) as actual:
        assert actual is failure
    else:
        raise AssertionError("capture import failure was hidden")
    assert executed == []
    assert tally.snapshot() == {}
    assert "base.telemetry" not in sys.modules


SCENARIOS = {
    "cold_import_loads_no_httpx": cold_import_loads_no_httpx,
    "capture_import_failure": capture_import_failure,
}

if __name__ == "__main__":
    SCENARIOS[sys.argv[1]]()
