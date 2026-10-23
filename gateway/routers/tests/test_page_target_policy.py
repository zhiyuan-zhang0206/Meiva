# meiva:note
# Which hosts and ports a page may be registered on and proxied to: values in, refusal out.

import pytest

from gateway.routers.page_serving.page_target_policy import (
    is_loopback_host,
    nonloopback_refusal,
    privileged_port_refusal,
    proxy_dial,
)
from gateway.routers.refusal import HttpFailure


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1", "127.9.9.9", "::ffff:127.0.0.1"])
def test_every_loopback_spelling_is_loopback(host: str) -> None:
    assert is_loopback_host(host)


@pytest.mark.parametrize("host", ["10.0.0.5", "runner-a", "foreign.internal", ""])
def test_other_hosts_are_not_loopback(host: str) -> None:
    assert not is_loopback_host(host)


def test_a_privileged_port_is_refused_before_anything_is_read() -> None:
    assert privileged_port_refusal(1023) == HttpFailure(
        400, "page port 1023 is privileged — page servers run on ports >= 1024"
    )
    assert privileged_port_refusal(1024) is None


def test_a_host_the_home_machine_advertises_is_allowed() -> None:
    assert (
        nonloopback_refusal("10.0.0.5", 7, "runner-a", {"runner-a", "10.0.0.5"}, status_code=400)
        is None
    )


def test_an_unadvertised_host_or_an_unknown_home_machine_fails_closed() -> None:
    foreign = nonloopback_refusal("foreign.internal", 7, "runner-a", {"runner-a"}, status_code=403)
    assert foreign is not None and foreign.status_code == 403
    assert "neither loopback nor 'runner-a'" in foreign.detail
    unknown = nonloopback_refusal("10.0.0.5", 7, None, frozenset(), status_code=400)
    assert unknown is not None and unknown.status_code == 400
    assert "home machine is unknown" in unknown.detail


# meiva:note
# The dial target is a registry-validated cluster host: a proxy-configured gateway host
# once forwarded its proxy's own 502 (2026-09-10), so the client ignores the environment.
def test_the_page_proxy_never_trusts_the_ambient_proxy_environment() -> None:
    dial = proxy_dial(connect_s=5.0, read_s=120.0)
    assert dial.trust_env is False
    assert (dial.connect_s, dial.read_s) == (5.0, 120.0)
