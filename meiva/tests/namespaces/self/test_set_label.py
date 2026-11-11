# meiva:note
# `meiva.self.set_label` is core: it hands the agent's own label to the gateway without any
# plugin installed. The Fleet plugin is deliberately never loaded here.
# meiva:link meiva/self/README.md

from __future__ import annotations

import pytest

import meiva
from meiva.tests.fixtures.gateway_wire import GatewayWire
from meiva.tests.fixtures.gateway_wire import gateway_stub as gateway_stub
from meiva.tests.fixtures.gateway_wire import wire as wire
from meiva.tests.fixtures.pin_agent import pin_agent


def test_set_label_is_a_core_member_without_any_plugin() -> None:
    assert callable(meiva.self.set_label)
    assert "set_label" in meiva.self.__all_for_meiva__
    assert meiva.self.set_label.__module__ == "meiva.self"


def test_set_label_hands_the_agents_own_label_to_the_gateway(wire: GatewayWire) -> None:
    wire.stub.reply("PATCH", "/api/agents/7", {})
    pin_agent(7, clients=wire.context.clients)

    meiva.self.set_label("auth-refactor lead")
    meiva.self.set_label("")

    assert [(sent.method, sent.path, sent.body) for sent in wire.sent] == [
        ("PATCH", "/api/agents/7", {"label": "auth-refactor lead", "source": "self"}),
        ("PATCH", "/api/agents/7", {"label": "", "source": "self"}),
    ]


# meiva:note
# A model passing a non-string is the case under test; the pyright ignore below is the deliberate
# wrong-type call.
def test_set_label_rejects_a_non_string_before_any_gateway_call(wire: GatewayWire) -> None:
    pin_agent(7, clients=wire.context.clients)

    with pytest.raises(TypeError):
        meiva.self.set_label(5)  # pyright: ignore[reportArgumentType]

    assert wire.sent == []
