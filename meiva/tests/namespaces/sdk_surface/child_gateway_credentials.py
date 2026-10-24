# meiva:note
# Child process for the gateway-credential test: endpoint and token are read at first use.


import os

from base.agents.context import MeivaContext
from base.agents.context.clients import ClientSet
from base.agents.context.identity import AgentIdentity
from base.config.tests.configuration import snapshot_process_config
from meiva.boot import context_from_description


def set_token(value: str) -> None:
    os.environ["MEIVA_API_TOKEN"] = value


host = MeivaContext(
    identity=AgentIdentity(17, True),
    clients=ClientSet(gateway_url="http://127.0.0.1:9"),
)
description = host.describe()
assert set(description) == {"identity", "gateway_url"}
config = snapshot_process_config()
os.environ["MEIVA_PROCESS_PROFILE"] = "agent"
set_token("before-overlay")
child = context_from_description(description, config=config)
set_token("after-overlay")
config.set_field("gateway_client_http_timeout_seconds", 17.0)
config.set_field("gateway_url", "http://unrelated.test")
try:
    client = child.gateway
    assert str(client.base_url).rstrip("/") == description["gateway_url"]
    assert client.headers["Authorization"] == "Bearer after-overlay"
    assert client.timeout.read == client.timeout.connect == 17.0
    assert "overlay" not in repr(description)
    child.clients.close()
    assert client.is_closed
    set_token("after-close")
    assert child.gateway.headers["Authorization"] == "Bearer after-close"
finally:
    child.clients.close()
