# meiva:note
# The e2e stack's credentials: a real cluster secret and a real write generation.
#
# The cluster API always authenticates, so the direct-process stack authenticates the
# way a started cluster does, with the same stores and none of the launcher's custody:
#
# - the e2e home gets a cluster secret (`MEIVA_CLUSTER_SECRET`, declared in its `.env`
#   like every cluster-scope key) and an active write generation in its private ledger
#   (`base.host.db_authority.tests.generation.seed_active_generation`, the ledger `meiva start`
#   births at first start);
# - the test process carries the generation marker (`MEIVA_DB_GENERATION`, the one a launcher
#   delivers beside the login), which tells the boot pass of every process descending from
#   it that its database login was delivered: this stack has no root custody to admit an
#   operator delivery from the ledger;
# - each process the stack starts is delivered the machine API token of its class in
#   `MEIVA_API_TOKEN`, as `cli.commands.data_plane.bringup.api_delivery` delivers it:
#   the gateway its gateway-class token (so its `/ops` dials are accepted), the ops
#   daemon and the agent host the runner-class token (agent children inherit it);
# - the test process reaches the gateway as an operator would, with the human secret as
#   a Bearer header (`gateway_get` and the other helpers below add it to a request that
#   names none).
#
# No product admission rule is bypassed or weakened for the fixture.


from __future__ import annotations

import secrets
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
from playwright.sync_api import Browser, BrowserContext

from base.config import set_field, settings
from base.host.db_authority.tests.generation import seed_active_generation
from base.host.net.auth import API_TOKEN_ENV, bearer_header
from tests.e2e._ports import GATEWAY_URL

__all__ = [
    "E2ECredentials",
    "current_credentials",
    "install_browser_login",
    "mint_credentials",
]


@dataclass(frozen=True)
class E2ECredentials:
    """The e2e home's human secret and its generation's machine API tokens."""

    secret: str
    gateway_token: str
    runner_token: str

    def service_env(self, cls: Literal["gateway", "runner"]) -> dict[str, str]:
        """The launch-environment delivery of one service class: its API token."""
        token = self.gateway_token if cls == "gateway" else self.runner_token
        return {API_TOKEN_ENV: token}


_current: E2ECredentials | None = None
_previous_secret = ""


def mint_credentials(home: Path) -> E2ECredentials:
    """Seed `home`'s ledger with an active generation and mint its human secret, which
    becomes this process's configured cluster secret until `forget_credentials`."""
    global _current, _previous_secret  # noqa: PLW0603 — package-scoped, set once by `e2e_process_env`
    tokens = seed_active_generation(home).api
    _previous_secret = settings.data_plane.cluster_secret
    _current = E2ECredentials(
        secret=secrets.token_urlsafe(32),
        gateway_token=tokens.gateway,
        runner_token=tokens.runner,
    )
    set_field("cluster_secret", _current.secret)
    return _current


def current_credentials() -> E2ECredentials:
    if _current is None:
        raise RuntimeError("the e2e credentials are minted by the `e2e_process_env` fixture")
    return _current


def forget_credentials() -> None:
    global _current  # noqa: PLW0603
    set_field("cluster_secret", _previous_secret)
    _current = None


__all__ = [
    "gateway_client",
    "gateway_delete",
    "gateway_get",
    "gateway_patch",
    "gateway_post",
    "gateway_put",
]


# meiva:note
# The operator's bearer, added to a request that carries no `Authorization` of its own and read
# per request: a test that mints a fresh generation gets that generation's secret without
# rebuilding its client.
class _OperatorBearer(httpx.Auth):
    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        if "authorization" not in request.headers:
            request.headers.update(bearer_header(current_credentials().secret))
        yield request


# meiva:note
# An `httpx.Client` that authenticates against the e2e gateway; every helper below is one call
# on it, so a test never builds the credential itself.
def gateway_client(**kwargs: Any) -> httpx.Client:
    kwargs.setdefault("timeout", 30.0)
    return httpx.Client(auth=_OperatorBearer(), **kwargs)


def _call(method: str, url: str, **kwargs: Any) -> httpx.Response:
    with gateway_client() as client:
        return client.request(method, url, **kwargs)


def gateway_get(url: str, **kwargs: Any) -> httpx.Response:
    return _call("GET", url, **kwargs)


def gateway_post(url: str, **kwargs: Any) -> httpx.Response:
    return _call("POST", url, **kwargs)


def gateway_put(url: str, **kwargs: Any) -> httpx.Response:
    return _call("PUT", url, **kwargs)


def gateway_patch(url: str, **kwargs: Any) -> httpx.Response:
    return _call("PATCH", url, **kwargs)


def gateway_delete(url: str, **kwargs: Any) -> httpx.Response:
    return _call("DELETE", url, **kwargs)


# meiva:note
# True once the gateway answers HTTP: the retained listening socket accepts
# connections before any process serves them, so only a response proves it. A
# session that never minted credentials runs without the stack (a suite that
# stubs every API call never starts one), so there is nothing to log in to, and
# the probe must answer without a request — it uses plain httpx, not the
# credential-carrying helpers, which would need the mint this probe is asking about.
# meiva:link docs/conventions/testing.md
def _gateway_serving() -> bool:
    if _current is None:
        return False
    try:
        return httpx.get(f"{GATEWAY_URL}/api/health", timeout=1.0).is_success
    except httpx.HTTPError:
        return False


# meiva:note
# Make every context this browser creates start logged in, as a user's browser is.
#
# The login is the link `meiva start` prints: the operator secret asks for a one-time ticket
# and the context redeems it. The gateway sets its session cookie on the redemption; it lives
# in the context's cookie jar and rides with the page's API calls. A context created while
# no gateway serves (a test that stubs the API and never starts one) has nothing to
# log in to and is left alone; once the gateway serves, a refused login fails loudly.
def install_browser_login(browser: Browser) -> None:
    create = browser.new_context

    def new_context(*args: Any, **kwargs: Any) -> BrowserContext:
        context = create(*args, **kwargs)
        if _gateway_serving():
            issued = context.request.post(
                f"{GATEWAY_URL}/api/auth/login-ticket",
                headers=bearer_header(current_credentials().secret),
            )
            assert issued.ok, f"e2e login ticket refused: {issued.status}"
            login = context.request.post(
                f"{GATEWAY_URL}/api/auth/login-ticket/redeem",
                data={"ticket": issued.json()["ticket"]},
            )
            assert login.ok, f"e2e browser login refused: {login.status}"
        return context

    browser.new_context = new_context  # pyright: ignore[reportAttributeAccessIssue]
