import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
from langchain_core.exceptions import ModelConnectionError
from langchain_core.messages import AIMessage, AIMessageChunk

from agent.graph.llm._deadlines import StreamBounds
from agent.graph.llm._retry import retry_policy, retry_wait
from agent.graph.llm._stream import consume_llm, consume_stream_with_stall_timeout
from agent.graph.llm.tests.llm_support import ScriptedModel, StubHandler
from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.lm.errors import is_retryable_provider_error

_ONE_SECOND = StreamBounds(ttft_seconds=1, inter_chunk_seconds=1, total_seconds=None)


@pytest.mark.parametrize("during_model_iteration", [False, True])
async def test_transport_authority_stays_at_the_model_boundary(
    during_model_iteration: bool,
) -> None:
    error = httpx.ConnectError("connection interrupted")

    async def stream() -> AsyncIterator[AIMessage]:
        if during_model_iteration:
            raise error
        yield AIMessageChunk(content="one chunk")

    handler = StubHandler(fail_with=None if during_model_iteration else error)
    expected = ModelConnectionError if during_model_iteration else httpx.ConnectError
    with pytest.raises(expected) as raised:
        await consume_stream_with_stall_timeout(
            stream(), chunks=[], handler=handler, bounds=_ONE_SECOND
        )
    if during_model_iteration:
        assert raised.value.__cause__ is error
        assert is_retryable_provider_error(raised.value)
    else:
        assert raised.value is error
        assert not is_retryable_provider_error(error)


@pytest.mark.parametrize("timeout_origin", ["stream_model", "stream_callback"])
async def test_builtin_timeout_does_not_borrow_owned_stall_authority(
    timeout_origin: str, model_catalog: ModelCatalog
) -> None:
    error = TimeoutError("application operation failed")
    model = ScriptedModel(
        scripts=[[error] if timeout_origin == "stream_model" else [AIMessageChunk(content="x")]]
    )
    handler = StubHandler(fail_with=error if timeout_origin == "stream_callback" else None)
    agent = AgentSlices.resolve(
        default_reader=defaults(
            lm__llm_stream_ttft_timeout_seconds=5.0, lm__llm_stream_inter_chunk_timeout_seconds=5.0
        )
    )

    with pytest.raises(TimeoutError) as raised:
        await consume_llm(model, [], chunks=[], handler=handler, agent=agent, catalog=model_catalog)

    assert raised.value is error
    policy = retry_policy(agent, model_catalog, 7)
    assert retry_wait(error, 1, policy=policy, jitter_seconds=0.0) is None
    assert model.scripts == []


async def test_external_cancellation_stays_external_inside_owned_model_deadline(
    model_catalog: ModelCatalog,
) -> None:
    model = ScriptedModel(scripts=[["hang"]])
    chunks: list[AIMessageChunk] = []
    task = asyncio.create_task(
        consume_llm(
            model,
            [],
            chunks=chunks,
            handler=StubHandler(),
            agent=AgentSlices.resolve(
                default_reader=defaults(
                    lm__llm_stream_ttft_timeout_seconds=10,
                    lm__llm_stream_inter_chunk_timeout_seconds=10,
                )
            ),
            catalog=model_catalog,
        )
    )
    async with asyncio.timeout(5):
        while model.scripts:
            await asyncio.sleep(0.005)
    task.cancel("user cancelled")

    with pytest.raises(asyncio.CancelledError) as raised:
        await task

    assert raised.value.args == ("user cancelled",)
    assert chunks == []
