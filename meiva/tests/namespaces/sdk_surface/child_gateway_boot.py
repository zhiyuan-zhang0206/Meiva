from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from base.agents.context import MeivaContext
from base.agents.context.identity import AgentIdentity
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry import process_logging as plog
from base.telemetry.delivery.pipeline import EventPipeline
from base.telemetry.tests.capture.event_capture import capturing_events
from meiva.sdk_surface import process_context


@dataclass(frozen=True)
class _OwnedImage:
    sha: str | None


mode = sys.argv[1]
process_context.bind_context(MeivaContext(identity=AgentIdentity(42, True)))
with capturing_events() as events:
    producer_calls = [0]

    def producer() -> EventPipeline:
        producer_calls[0] += 1
        return events.producer()

    image: plog.LoadedImage
    if mode == "loaded-commit":
        image = LoadedCommit(Path(__file__).resolve().parents[4], "loaded-generation")
        host = "machine-a"
    else:
        image = _OwnedImage(sha="already-loaded")
        host = "owned-machine"

    def read_machine() -> str:
        return host

    plog.init_gateway_process(
        name="agent_host",
        producer=producer,
        machine_reader=read_machine,
        image=image,
    )
    [started] = events.named("service_started")
    print(
        json.dumps(
            {
                "agent_id": started.agent_id,
                "process": started.process,
                "machine": started.machine,
                "attributes": started.attributes,
                "producer_calls": producer_calls[0],
            }
        )
    )
