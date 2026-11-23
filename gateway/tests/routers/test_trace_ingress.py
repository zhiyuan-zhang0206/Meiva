from collections.abc import Callable
from pathlib import Path

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from starlette.testclient import TestClient

from base.host.db_authority.model import GenerationSecret
from base.host.net.auth import bearer_header
from gateway.tests.auth.gateway_config import gateway_test_app, gateway_test_process

_EXTERNAL_TRACE = "102030405060708090a0b0c0d0e0f000"
_EXTERNAL_PARENT = "face0000feed0000"
_HUMAN_BEARER = "trace-ingress-human-credential"


@pytest.mark.parametrize(
    ("caller", "flags", "status"),
    [("machine", "01", 200), ("human", "00", 200), ("unverified", "01", 401)],
)
def test_gateway_accepts_trace_parent_only_from_machine_bearer(
    tmp_path: Path,
    seed_write_generation: Callable[[Path], GenerationSecret],
    caller: str,
    flags: str,
    status: int,
) -> None:
    home = tmp_path / "gateway"
    tokens = seed_write_generation(home).api
    exporter = InMemorySpanExporter()
    with gateway_test_process(home=home) as process:
        provider = TracerProvider(shutdown_on_exit=False)
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        process.tracer_provider = provider
        app = gateway_test_app(process=process, cluster_secret=_HUMAN_BEARER)

        @app.get("/api/trace-probe")
        async def probe() -> str:
            context = trace.get_current_span().get_span_context()
            return f"{context.trace_id:032x}"

        credential = {"machine": tokens.runner, "human": _HUMAN_BEARER, "unverified": "invalid"}[
            caller
        ]
        with TestClient(app) as client:
            response = client.get(
                "/api/trace-probe",
                headers={
                    **bearer_header(credential),
                    "traceparent": f"00-{_EXTERNAL_TRACE}-{_EXTERNAL_PARENT}-{flags}",
                    "tracestate": "vendor=client",
                },
            )

    assert response.status_code == status
    [span] = exporter.get_finished_spans()
    assert span.context is not None
    if caller == "machine":
        assert span.parent is not None
        assert f"{span.parent.span_id:016x}" == _EXTERNAL_PARENT
        assert f"{span.context.trace_id:032x}" == _EXTERNAL_TRACE
        assert span.context.trace_state.get("vendor") == "client"
    else:
        assert span.parent is None
        assert f"{span.context.trace_id:032x}" != _EXTERNAL_TRACE
        assert span.context.trace_state.get("vendor") is None
    if status == 200:
        assert response.json() == f"{span.context.trace_id:032x}"
