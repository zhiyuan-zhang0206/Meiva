import random
from pathlib import Path

import httpx2
from starlette.testclient import TestClient

from base.host.net.auth import bearer_header
from gateway.http.auth.login_password_store import LoginPasswords
from gateway.http.config import GatewayConfig

__all__ = [
    "LOGIN_PASSWORD",
    "gateway_credentials",
    "login_with_ticket",
    "seed_login_password",
    "without_credentials",
]

# meiva:note
# A fixture value, not a credential.
LOGIN_PASSWORD = "test-login-password"  # noqa: S105


def gateway_credentials(config: GatewayConfig) -> dict[str, str]:
    return bearer_header(config.cluster_secret)


def without_credentials(client: TestClient) -> TestClient:
    client.headers.pop("Authorization", None)
    return client


# meiva:note
# Store `password` as the web login password of `home`, the file a `PUT /api/auth/password`
# leaves: for a test that needs a browser to log in and is not about setting the password. The
# gateway reads the record from the home it serves, so the test gives that home (a per-test
# `unit_home`, never the session's shared one, which every later test would inherit).
def seed_login_password(home: Path, password: str = LOGIN_PASSWORD) -> None:
    # meiva:note
    # A seeded generator: the salt of a test record needs no entropy.
    LoginPasswords(random.Random(0)).set(home, password)  # noqa: S311


# meiva:note
# Open a browser session the way the link `meiva start` prints does: a one-time ticket issued to
# the caller presenting `secret` (the cluster secret, the credential the session is then bound
# to), redeemed for the session cookie. For a test that needs a browser logged in as the human
# secret and is not about the web login password. Returns the redeem response; the client's
# cookie jar holds the cookie.
def login_with_ticket(client: TestClient, secret: str) -> httpx2.Response:
    issued = client.post("/api/auth/login-ticket", headers=bearer_header(secret))
    assert issued.status_code == 200, issued.text
    return client.post("/api/auth/login-ticket/redeem", json={"ticket": issued.json()["ticket"]})
