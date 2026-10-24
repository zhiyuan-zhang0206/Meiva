# meiva:note
# The credential a process presents to the gateway API is a pure function of its `GatewayAccess`.


from __future__ import annotations

import pytest

from base.cluster import machine
from base.cluster.machine import GatewayAccess, gateway_auth_headers
from base.host.env.process_profile import ProcessProfile
from base.host.net.auth import bearer_header

SECRET = "human-" + "h" * 40
TOKEN = "delivered-" + "t" * 32


def _access(
    secret: str,
    *,
    profile: ProcessProfile | None = None,
    token: str | None = None,
    remote_plane: bool = False,
    gateway_home: bool = False,
) -> GatewayAccess:
    return GatewayAccess(
        gateway_url="http://gateway.test:8000",
        cluster_secret=secret,
        remote_plane=remote_plane,
        machine_token=token,
        launcher_profile=profile,
        gateway_home=gateway_home,
    )


@pytest.mark.parametrize("profile", [None, "gateway", "agent", "runner"])
def test_a_delivered_token_is_presented_before_the_secret(profile: ProcessProfile | None) -> None:
    access = _access(SECRET, profile=profile, token=TOKEN)
    assert gateway_auth_headers(access) == bearer_header(TOKEN)


@pytest.mark.parametrize("profile", [None, "gateway", "agent", "runner"])
def test_a_process_with_no_credential_fails_instead_of_presenting_nothing(
    profile: ProcessProfile | None,
) -> None:
    with pytest.raises(machine.GatewayApiTokenMissing, match="MEIVA_API_TOKEN"):
        gateway_auth_headers(_access("", profile=profile))


@pytest.mark.parametrize("profile", [None, "gateway"])
def test_operator_and_gateway_processes_without_a_token_present_the_secret(
    profile: ProcessProfile | None,
) -> None:
    assert gateway_auth_headers(_access(SECRET, profile=profile)) == bearer_header(SECRET)


@pytest.mark.parametrize("profile", ["agent", "runner"])
def test_agent_and_runner_processes_without_a_token_fail_instead_of_using_the_secret(
    profile: ProcessProfile,
) -> None:
    with pytest.raises(RuntimeError, match="MEIVA_API_TOKEN") as raised:
        gateway_auth_headers(_access(SECRET, profile=profile))

    message = str(raised.value)
    assert f"{profile}-profile" in message
    assert SECRET not in message


def test_the_failure_is_the_named_error() -> None:
    with pytest.raises(machine.GatewayApiTokenMissing):
        machine.gateway_bearer(_access(SECRET, profile="agent"))


@pytest.mark.parametrize("profile", [None, "gateway", "agent", "runner"])
def test_a_remote_managed_gateway_home_keeps_presenting_the_secret(
    profile: ProcessProfile | None,
) -> None:
    access = _access(SECRET, profile=profile, remote_plane=True, gateway_home=True)
    assert gateway_auth_headers(access) == bearer_header(SECRET)


def test_a_unit_that_does_not_serve_the_gateway_is_never_exempt() -> None:
    access = _access(SECRET, profile="runner", remote_plane=True, gateway_home=False)
    with pytest.raises(RuntimeError, match="MEIVA_API_TOKEN"):
        gateway_auth_headers(access)


def test_a_gateway_home_on_a_local_plane_is_not_exempt() -> None:
    access = _access(SECRET, profile="agent", remote_plane=False, gateway_home=True)
    with pytest.raises(machine.GatewayApiTokenMissing):
        gateway_auth_headers(access)


def test_the_secret_and_the_token_never_reach_a_repr() -> None:
    shown = repr(_access(SECRET, token=TOKEN))
    assert SECRET not in shown
    assert TOKEN not in shown
