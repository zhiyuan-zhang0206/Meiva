"""Cold-interpreter SDK telemetry scenarios; never imported by a production entry.

Run as ``python -I _sdk_cold_child.py <scenario>``. ``capture_import_failure`` reads the injected
error class from ``TEST_CAPTURE_ERROR``.
"""

from __future__ import annotations

import sys


def cold_import_loads_no_httpx() -> None:
    import base.agents.sdk.telemetry  # noqa: F401  # pyright: ignore[reportUnusedImport]

    assert "httpx" not in sys.modules
    assert "httpcore" not in sys.modules


# meiva:note
# Run `SamplingPolicyOwner` refresh in a bare subprocess without `prepare_process_logging` or
# `add_sink` to verify default handler 0 does not render frame variables (#6594).
def bare_refresh_outage() -> None:
    import os
    import random
    from pathlib import Path

    from base.agents.sdk.call_policy import SamplingPolicy, SamplingPolicyOwner, SamplingSource
    from base.host.env.bootstrap import BootstrapFetch
    from base.log.diagnose import diagnosing_handler_ids

    source = SamplingSource(
        env=os.environ,
        env_path=Path(os.environ["MEIVA_HOME"]) / ".env",
        refresh_seconds=0.0,
        tuning=BootstrapFetch(attempts=1, timeout_s=0.2, backoff_step_s=0.0),
    )
    owner = SamplingPolicyOwner(
        initial=SamplingPolicy(),
        source=source,
        rng=random.Random(0),  # noqa: S311
    )
    _ = owner.read()
    assert owner.worker is not None and owner.worker.completed.wait(10)
    _ = sys.stdout.write(f"diagnosing: {diagnosing_handler_ids()}\n")
    _ = owner.close()


SCENARIOS = {
    "cold_import_loads_no_httpx": cold_import_loads_no_httpx,
    "bare_refresh_outage": bare_refresh_outage,
}

if __name__ == "__main__":
    SCENARIOS[sys.argv[1]]()
