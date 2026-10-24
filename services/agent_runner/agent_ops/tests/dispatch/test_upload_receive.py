# meiva:note
# The real receiver must classify source refusals from the production HTTP client.
from __future__ import annotations

import httpx
import pytest

from base.agents.upload_delivery.models import ReceiveRequest, current_unit, make_manifest
from base.config import get_field, set_field
from base.host.net.tests.local_http import serve_json
from ops.rpc_schemas import OpStatus
from services.agent_runner.agent_ops.tests.ops_support import Ops


@pytest.mark.parametrize("status_code", [401, 403, 404, 409])
async def test_upload_receive_classifies_real_source_refusals(
    ops: Ops, agent_id: int, status_code: int
) -> None:
    target = current_unit(local_machine=ops.machine.name())
    manifest = make_manifest(agent_id, ["source.txt"], [("source.txt", b"source", "text/plain")])
    request = ReceiveRequest(version=1, target=target, source=target, manifest=manifest)
    previous_url = get_field("gateway_url")
    with serve_json({"error": "source unavailable"}, status=status_code) as source:
        set_field("gateway_url", source.url)
        try:
            status, result = await ops.dispatch("upload-receive-v1", request.model_dump())
        finally:
            set_field("gateway_url", previous_url)

    assert status is OpStatus.FAILED
    assert result == {
        "error": "source object unavailable",
        "reason": "upload-source-unavailable-v1",
    }


async def test_upload_receive_keeps_server_errors_distinct_from_source_refusals(
    ops: Ops, agent_id: int
) -> None:
    target = current_unit(local_machine=ops.machine.name())
    manifest = make_manifest(agent_id, ["source.txt"], [("source.txt", b"source", "text/plain")])
    request = ReceiveRequest(version=1, target=target, source=target, manifest=manifest)
    previous_url = get_field("gateway_url")
    with serve_json({"error": "temporarily unavailable"}, status=503) as source:
        set_field("gateway_url", source.url)
        try:
            with pytest.raises(httpx.HTTPStatusError) as raised:
                await ops.dispatch("upload-receive-v1", request.model_dump())
        finally:
            set_field("gateway_url", previous_url)

    assert raised.value.response.status_code == 503
