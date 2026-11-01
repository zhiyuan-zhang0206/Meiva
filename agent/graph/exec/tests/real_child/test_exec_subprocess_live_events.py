from __future__ import annotations

from pathlib import Path

from agent.graph.exec.node import ExecDone, ExecOutputChunkPublisher
from agent.graph.exec.tests.real_child.test_exec_subprocess import AGENT_ID, run_exec_child
from base.events.live.tests.live_capture import LiveEvents


async def test_subprocess_streaming_chunks_published_incrementally(
    tmp_path: Path, live_events: LiveEvents
) -> None:
    publisher = ExecOutputChunkPublisher(live_events.publisher, agent_id=AGENT_ID, item_id="7.0")
    result = await run_exec_child(
        tmp_path,
        "import time\nfor i in range(5):\n    print(f'line {i}')\n    time.sleep(0.15)",
        timeout=30.0,
        chunk_publisher=publisher,
    )
    assert isinstance(result, ExecDone)
    chunks = [event for event in await live_events.events() if not event["keepalive"]]
    assert len(chunks) >= 2
    assert "".join(event["content"] for event in chunks) == result.output
    assert all(event["item_id"] == "7.0" for event in chunks)


async def test_subprocess_silent_child_publishes_keepalive(
    tmp_path: Path, live_events: LiveEvents
) -> None:
    publisher = ExecOutputChunkPublisher(live_events.publisher, agent_id=AGENT_ID, item_id="7.0")

    result = await run_exec_child(
        tmp_path,
        "import time; time.sleep(1.3)",
        timeout=30.0,
        chunk_publisher=publisher,
    )

    assert isinstance(result, ExecDone)
    assert result.output == ""
    events = await live_events.events()
    keepalives = [event for event in events if event["keepalive"] is True]
    assert keepalives
    assert all(event["content"] == "" for event in keepalives)
