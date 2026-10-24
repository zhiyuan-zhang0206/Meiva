import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from loguru import logger

from agent.execution import child
from agent.execution.protocol import ResultPayload
from base.agents.sdk import call_policy
from base.agents.sdk.tests.sampling_owners import gateway_sampling_owner
from base.host.net.tests.local_http import serve_json
from base.packages.plugin_config_images import DiskPluginConfigImages
from base.packages.plugins.extensions import ExtensionRegistry, PluginContributions, SdkNamespace
from base.paths.home import resolve_meiva_home
from meiva.sdk_surface import install
from meiva.tests.fixtures.pin_agent import pin_agent


def _assert_outcome(
    payload: ResultPayload,
    outcome: str,
    sampling: call_policy.SamplingPolicyOwner,
    body_error: ValueError,
    reports: list[str],
) -> None:
    if outcome == "blocked":
        assert payload.kind == "done"
        assert sampling.worker is not None and sampling.worker.thread.is_alive()
        assert any("refresh unfinished" in report for report in reports)
    elif outcome == "body_failure":
        assert payload.kind == "crashed" and payload.exc_type == "ValueError"
        assert payload.full_traceback is not None
        assert "HTTPStatusError" in payload.full_traceback
        assert body_error.__notes__ and "shutdown also failed" in body_error.__notes__[0]
    else:
        assert payload.kind == "crashed" and payload.exc_type == "HTTPStatusError"
        assert payload.exc_msg is not None and "500" in payload.exc_msg


@pytest.mark.usefixtures("emitted_events")
@pytest.mark.parametrize("outcome", ["blocked", "body_failure", "refresh_failure"])
def test_run_code_collects_sampling_at_its_result_boundary(outcome: str, tmp_path: Path) -> None:
    gate = threading.Event()
    body_error = ValueError("agent body failed")
    status = 200 if outcome == "blocked" else 500
    with serve_json({}, status=status, gate=gate) as gateway:
        sampling = gateway_sampling_owner(gateway.url, tmp_path / ".env", timeout_s=10.0)

        def body() -> None:
            assert gateway.arrived.wait(2)
            if outcome != "blocked":
                gate.set()
                assert sampling.worker is not None and sampling.worker.completed.wait(5)
            if outcome == "body_failure":
                raise body_error

        install.uninstall()
        install.install(
            ExtensionRegistry(
                (
                    (
                        "probe",
                        PluginContributions(
                            sdk_namespaces=(SdkNamespace("probe", SimpleNamespace(body=body)),)
                        ),
                    ),
                )
            ),
            sampling=sampling,
            config_images=DiskPluginConfigImages(resolve_meiva_home()),
        )
        pin_agent(37)
        payload = ResultPayload(kind="done")
        reports: list[str] = []
        sink = logger.add(lambda message: reports.append(str(message)))
        try:
            child.run_code("import meiva; meiva.probe.body()", payload)
            assert payload.code_reached
            _assert_outcome(payload, outcome, sampling, body_error, reports)
            assert sampling.error is None or isinstance(sampling.error[0], httpx.HTTPStatusError)
        finally:
            gate.set()
            if sampling.worker is not None:
                assert sampling.worker.completed.wait(5)
                assert sampling.worker.stop(2)
            logger.remove(sink)
            install.uninstall()
