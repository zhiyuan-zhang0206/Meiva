# meiva:note
# The lease's declared capabilities gate the SDK calls a controller makes. The gate
# lives on one seam - `Attachment.admit_sdk_call`, called for every public `meiva.*`
# entry by the usage recorder. These tests pin its decisions without a database: the
# three stored states (NULL, an array, []), the three enforcement modes, and the
# unclassified entry that must never pass silently in `enforce`.

from __future__ import annotations

from typing import Any

import pytest

from base.agents.impersonation.capabilities import Capability, CapabilityDenied
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from meiva.impersonation.attachment import Attachment

_REFUSED = "impersonation_capability_refused"


# meiva:note
# A gate-only attachment: the method under test touches only these fields.
def _attachment(mode: str) -> Attachment:
    attachment = object.__new__(Attachment)
    attachment.agent_id = 7
    attachment.lease_id = "00000000-0000-0000-0000-000000000007"
    attachment._capability_mode_reader = lambda: mode
    return attachment


# meiva:note
# A lease issued before the vocabulary existed: it keeps its full surface.
def _legacy() -> dict[str, Any]:
    return {"id": "lease", "capabilities": None}


def _scoped(*granted: Capability) -> dict[str, Any]:
    return {"id": "lease", "capabilities": [capability.value for capability in granted]}


def test_a_legacy_lease_runs_everything_even_in_enforce(emitted_events: EmittedEvents) -> None:
    attachment = _attachment("enforce")

    attachment._check_capability("agents.spawn", _legacy())
    attachment._check_capability("context.anything", _legacy())

    assert emitted_events.names() == []


def test_a_declared_capability_passes(emitted_events: EmittedEvents) -> None:
    attachment = _attachment("enforce")

    attachment._check_capability("files.read", _scoped(Capability.READ, Capability.AGENTS_MESSAGE))

    assert emitted_events.names() == []


def test_an_empty_declaration_grants_nothing(emitted_events: EmittedEvents) -> None:
    attachment = _attachment("enforce")

    with pytest.raises(CapabilityDenied) as refused:
        attachment._check_capability("files.read", _scoped())

    assert refused.value.capability is Capability.READ
    assert refused.value.entry == "files.read"


def test_warn_records_the_call_and_lets_it_run(emitted_events: EmittedEvents) -> None:
    attachment = _attachment("warn")

    attachment._check_capability("agents.spawn", _scoped(Capability.READ))

    assert emitted_events.attributes(_REFUSED) == [
        {
            "lease_id": "lease",
            "entry": "agents.spawn",
            "capability": "agents.spawn",
            "mode": "warn",
        }
    ]
    assert emitted_events.named(_REFUSED)[0].level == "warning"


def test_enforce_records_the_call_and_refuses_it(emitted_events: EmittedEvents) -> None:
    attachment = _attachment("enforce")

    with pytest.raises(CapabilityDenied) as refused:
        attachment._check_capability("agents.terminate", _scoped(Capability.READ))

    assert refused.value.capability is Capability.AGENTS_CONTROL
    assert emitted_events.attributes(_REFUSED)[0]["mode"] == "enforce"
    assert emitted_events.named(_REFUSED)[0].level == "error"


def test_an_unclassified_entry_is_recorded_with_no_capability(
    emitted_events: EmittedEvents,
) -> None:
    attachment = _attachment("warn")

    attachment._check_capability("some.future.entry", _scoped(Capability.READ))

    assert emitted_events.attributes(_REFUSED)[0]["capability"] == ""
    assert emitted_events.attributes(_REFUSED)[0]["entry"] == "some.future.entry"


def test_an_unclassified_entry_is_refused_in_enforce(emitted_events: EmittedEvents) -> None:
    attachment = _attachment("enforce")

    with pytest.raises(CapabilityDenied) as refused:
        attachment._check_capability("some.future.entry", _scoped(Capability.READ))

    assert refused.value.capability is None
    assert emitted_events.attributes(_REFUSED)[0]["capability"] == ""


def test_off_neither_records_nor_refuses(emitted_events: EmittedEvents) -> None:
    attachment = _attachment("off")

    attachment._check_capability("agents.spawn", _scoped())

    assert emitted_events.names() == []


def test_an_unknown_mode_fails_loudly(emitted_events: EmittedEvents) -> None:
    attachment = _attachment("lenient")

    with pytest.raises(RuntimeError, match="impersonation_capability_enforcement"):
        attachment._check_capability("files.read", _scoped())
