# meiva:note
# UI writes use the resolved caller and runtime retry identity with controlled HTTP only.

from collections.abc import Iterator
from dataclasses import replace
from uuid import NAMESPACE_URL, uuid5

import httpx
import pytest

from base.agents.context import MeivaContext
from base.agents.context.clients import ClientSet
from base.agents.context.gateway_client.transport import GatewayTransportInputs
from base.agents.context.identity import AgentIdentity, ExternalLease
from base.agents.sdk.operation_ids import ExecutionIdentity, OperationIds
from meiva import ui
from meiva.sdk_surface import process_context


@pytest.fixture
def restore_context() -> Iterator[None]:
    previous = process_context.unbind_context()
    try:
        yield
    finally:
        process_context.unbind_context()
        if previous is not None:
            process_context.bind_context(previous)


@pytest.mark.usefixtures("restore_context")
@pytest.mark.parametrize("external", [False, True])
def test_ui_batch_and_retry_use_the_resolved_native_or_external_identity(external: bool) -> None:
    requests: list[httpx.Request] = []
    admissions: list[int] = []
    revoked = False

    def validate() -> int:
        if revoked:
            raise RuntimeError("attachment expired")
        admissions.append(41)
        return 41

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"ids": [11, 12]}
            if request.url.path.endswith("/asks")
            else {
                "id": 19,
                "content": "Milestone",
                "suggestions": [],
                "agent_id": 41,
                "mode": "sdk",
                "source_key": "controlled",
                "created_at": "2026-11-12T00:00:00Z",
                "seen": False,
            },
        )

    with httpx.Client(
        base_url="http://controlled", transport=httpx.MockTransport(answer)
    ) as client:
        context = MeivaContext(
            identity=AgentIdentity(
                agent_id=None if external else 41,
                owns_loop=not external,
                lease=ExternalLease(agent_id=41, validate=validate, config=lambda: None)
                if external
                else None,
            ),
            clients=ClientSet(
                gateway_url="http://controlled",
                gateway=lambda _: client,
                factories={
                    GatewayTransportInputs: lambda: GatewayTransportInputs(
                        max_retries_reader=lambda: 1,
                        retry_delay_reader=lambda: 0,
                        memory_deadline_reader=lambda: 4,
                    )
                },
            ),
        )

        def replay() -> None:
            process_context.bind_context(
                replace(
                    context,
                    operation_ids=OperationIds(
                        ExecutionIdentity(uuid5(NAMESPACE_URL, "ui-test"), None)
                    ),
                )
            )

        questions: list[dict[str, object]] = [
            {"title": "First"},
            {"title": "Second", "note_suggestion": "Soon"},
        ]
        replay()
        assert ui.ask(questions) == [11, 12]
        replay()
        assert ui.ask(questions) == [11, 12]
        assert requests[0].headers["Idempotency-Key"] == requests[1].headers["Idempotency-Key"]
        assert all(r.url.path == "/api/agents/41/asks" for r in requests)
        assert b'"type":"text"' in requests[0].content
        assert ui.publish("Milestone") == 19
        ui.close_question(11)
        count = len(requests)
        with pytest.raises(ValueError):
            ui.ask([{"title": "x", "unknown": True}])
        assert len(requests) == count
        if external:
            assert admissions
            revoked = True
            with pytest.raises(RuntimeError, match="attachment expired"):
                ui.ask(questions)
            assert len(requests) == count
