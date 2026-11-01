# meiva:note
# The real receiver must classify source refusals from the production HTTP client.
from __future__ import annotations

import hashlib
from dataclasses import replace

import httpx
import pytest

from base.agents.upload_delivery.models import PROTOCOL, Manifest, Object, ReceiveRequest
from base.host.net.tests.local_http import serve_json
from ops.rpc_schemas import OpStatus
from services.agent_runner.agent_ops.tests.ops_support import Ops


# meiva:note
# The same ops, whose upload receiver fetches from the gateway at `url`.
def _from(ops: Ops, url: str) -> Ops:
    receiver = replace(ops.receiver, access=replace(ops.receiver.access, gateway_url=url))
    return replace(ops, receiver=receiver)


def _manifest(agent_id: int) -> Manifest:
    data = b"source"
    return Manifest(
        batch_id="a" * 32,
        agent_id=agent_id,
        sent_at="2026-04-19T14:23:05+00:00",
        directory="20260419-142305",
        objects=[
            Object(
                ordinal=0,
                name="source.txt",
                staged_id="0" * 32,
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                content_type="text/plain",
            )
        ],
    )


@pytest.mark.parametrize("status_code", [401, 403, 404, 409])
async def test_upload_receive_classifies_real_source_refusals(
    ops: Ops, agent_id: int, status_code: int
) -> None:
    target = ops.receiver.unit
    request = ReceiveRequest(
        version=PROTOCOL, target=target, source=target, manifest=_manifest(agent_id)
    )
    with serve_json({"error": "source unavailable"}, status=status_code) as source:
        status, result = await _from(ops, source.url).dispatch(
            "upload-receive-v2", request.model_dump()
        )

    assert status is OpStatus.FAILED
    assert result == {
        "error": "source object unavailable",
        "reason": "upload-source-unavailable-v1",
    }


async def test_upload_receive_keeps_server_errors_distinct_from_source_refusals(
    ops: Ops, agent_id: int
) -> None:
    target = ops.receiver.unit
    request = ReceiveRequest(
        version=PROTOCOL, target=target, source=target, manifest=_manifest(agent_id)
    )
    with (
        serve_json({"error": "temporarily unavailable"}, status=503) as source,
        pytest.raises(httpx.HTTPStatusError) as raised,
    ):
        await _from(ops, source.url).dispatch("upload-receive-v2", request.model_dump())

    assert raised.value.response.status_code == 503
