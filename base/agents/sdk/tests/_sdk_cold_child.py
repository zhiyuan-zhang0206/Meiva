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


SCENARIOS = {
    "cold_import_loads_no_httpx": cold_import_loads_no_httpx,
}

if __name__ == "__main__":
    SCENARIOS[sys.argv[1]]()
