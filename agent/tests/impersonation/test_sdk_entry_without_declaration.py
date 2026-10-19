# meiva:note
# A lease's stored capability declaration is a record, never a gate. The user ruled
# (2026-10-19) that a takeover declares nothing and may still use every SDK entry, so
# the one acceptance property is: with the strictest stored declaration - an empty
# array - every public `meiva.*` entry is still admitted. The check runs on the seam
# every entry passes through (the metering recorder -> `Attachment.admit_sdk_call`),
# not on a helper that answers a boolean: each entry is driven through the real
# recorder against a real, attached `Attachment`. Only the entry's body is replaced
# with a no-op, because the walk must not perform the real operations the whole
# surface exposes.

from __future__ import annotations

import dataclasses
from typing import Any

from agent.tests.impersonation.external_attachment import (
    attached_mcp_runtime as attached_mcp_runtime,
)
from agent.tests.impersonation.external_attachment import attached_runtime as attached_runtime
from base.agents.context.clients import ClientSet
from base.agents.sdk import call_policy
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from meiva.impersonation import attachment as attachment_module
from meiva.sdk_surface import metering, process_context


def test_a_lease_that_declares_no_capability_reaches_every_sdk_entry(
    attached_mcp_runtime: tuple[dict[str, Any], attachment_module.Attachment],
    emitted_events: EmittedEvents,
) -> None:
    lease, attachment = attached_mcp_runtime
    lease["capabilities"] = []
    lease["capability_declared_by"] = "caller-default"
    bound = process_context.current_context()
    # meiva:note
    # The recorder hands every metered entry's admission to the bound context's owner:
    # without this the walk below would prove nothing about the lease.
    assert bound.sdk_capture is attachment
    process_context.bind_context(
        dataclasses.replace(bound, clients=ClientSet(pipeline_factory=emitted_events.producer))
    )
    executed: list[str] = []

    def body() -> str:
        executed.append("ran")
        return "ran"

    sampling = call_policy.SamplingPolicyOwner()
    try:
        entries = sorted(fq for _parent, _attr, fq in metering.instrument_targets())
        assert len(entries) > 40, (
            f"the surface walk found only {len(entries)} entries: it is looking in the wrong place"
        )
        # meiva:note
        # The walk itself resolves the dynamic `mcps` namespace through one metered call;
        # drop it so the events below are exactly the entries this test drove.
        emitted_events.clear()
        for entry in entries:
            assert metering.make_recorder(body, entry, sampling)() == "ran", entry
    finally:
        sampling.close()

    assert len(executed) == len(entries)
    assert sorted(event.attributes["fn"] for event in emitted_events.named("sdk_call")) == entries
