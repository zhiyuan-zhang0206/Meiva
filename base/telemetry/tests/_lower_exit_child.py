"""Clean-interpreter exit scenarios for the lower telemetry owners; never imported by a production entry.

Run as ``python -I _lower_exit_child.py <cold|loaded>`` with ``MEIVA_HOME`` set by the parent.
"""

from __future__ import annotations

import atexit
import sys
import threading


def cold() -> None:
    def observe() -> None:
        assert "base.telemetry.tracing" not in sys.modules
        assert "base.telemetry.otlp.telemetry_otlp" not in sys.modules
        assert "opentelemetry.sdk" not in sys.modules
        assert not any(
            t.name in {"trace-arm", "trace-collector-retry", "otlp-exporter"}
            for t in threading.enumerate()
        )
        sys.stdout.write("cold exit observed\n")

    atexit.register(observe)
    from base import telemetry

    assert telemetry.process_state["pipeline"] is None


def loaded() -> None:
    import socket

    from base.cluster.machine import MachineFacts
    from base.paths import meiva_home
    from base.telemetry.otlp.trace_config import TraceConfig
    from base.telemetry.otlp.trace_readiness import PROCESS_RECORDING
    from base.telemetry.otlp.trace_workers import CollectorRetry

    owners: list[CollectorRetry] = []

    def observe() -> None:
        from base.telemetry import tracing

        assert tracing.state["closed"]
        assert PROCESS_RECORDING.resolved and not PROCESS_RECORDING.ready
        assert len(owners) == 1
        assert owners[0].finished.is_set()
        assert not owners[0]._thread.is_alive()
        sys.stdout.write("loaded trace exit observed\n")

    atexit.register(observe)
    from base.telemetry import tracing

    # meiva:note
    # A bound but non-listening socket owns an actually unavailable endpoint. The real
    # collector preflight admits one retry worker; the registered exit hook must wake it.
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        port = unavailable.getsockname()[1]
        tracing.initialize_tracing(
            machine=MachineFacts.of(name="exit-test", role={"agent-runner"}),
            home=meiva_home(),
            config=TraceConfig(True, False, f"http://127.0.0.1:{port}", 3, 2048, 1.0, True),
            readiness=PROCESS_RECORDING,
        )
        owner = tracing.state["retry_owner"]
        assert isinstance(owner, CollectorRetry)
        assert not owner.finished.is_set()
        owners.append(owner)


if __name__ == "__main__":
    {"cold": cold, "loaded": loaded}[sys.argv[1]]()
