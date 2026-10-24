from starlette.testclient import TestClient

from base.host.net.auth import bearer_header
from gateway.http.config import GatewayConfig

__all__ = [
    "gateway_credentials",
    "without_credentials",
]


def gateway_credentials(config: GatewayConfig) -> dict[str, str]:
    return bearer_header(config.cluster_secret)


def without_credentials(client: TestClient) -> TestClient:
    client.headers.pop("Authorization", None)
    return client
