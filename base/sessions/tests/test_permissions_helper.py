import pytest

from base.native_process.os_platform import Platform
from base.sessions.helper_chain_guard import (
    HELPER_PID_ENV,
    helper_marker_pid,
    parent_chain_intact,
)
from base.sessions.helperproc import helper_client, helper_for


def _platform(sys_platform: str) -> Platform:
    return Platform.from_sys_platform(sys_platform, machine="arm64", executable="/usr/bin/python3")


def test_macos_always_runs_with_the_real_helper_factory() -> None:
    assert helper_for(_platform("darwin")) is helper_client


def test_linux_has_no_helper() -> None:
    assert helper_for(_platform("linux")) is None


def test_an_unknown_platform_is_refused_not_given_a_default() -> None:
    with pytest.raises(ValueError, match="unsupported platform"):
        _platform("freebsd")
    platform = _platform("linux")
    object.__setattr__(platform, "system", "freebsd")
    with pytest.raises(ValueError, match=r"no permissions-helper decision.*freebsd"):
        helper_for(platform)


def test_a_process_without_the_marker_is_left_alone() -> None:
    assert helper_marker_pid({}) is None
    assert parent_chain_intact(10, environ={}, ppids={10: 7, 7: 1})


def test_the_marker_is_read_from_the_environment_it_is_given() -> None:
    assert helper_marker_pid({HELPER_PID_ENV: "5"}) == "5"


def test_a_chain_that_reaches_the_helper_is_intact() -> None:
    assert parent_chain_intact(10, environ={HELPER_PID_ENV: "5"}, ppids={10: 7, 7: 5})


def test_a_chain_that_never_reaches_the_helper_is_broken() -> None:
    assert not parent_chain_intact(10, environ={HELPER_PID_ENV: "5"}, ppids={10: 7, 7: 1})


@pytest.mark.parametrize("marker", ["not-a-pid", "0", "-3", ""])
def test_a_malformed_marker_is_a_broken_chain_not_a_disabled_guard(marker: str) -> None:
    assert not parent_chain_intact(10, environ={HELPER_PID_ENV: marker}, ppids={10: 5})
