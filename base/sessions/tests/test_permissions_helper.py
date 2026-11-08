from functools import partial
from pathlib import Path

import pytest

from base.native_process.os_platform import Platform
from base.sessions.helperproc import helper_client, helper_for


def _platform(sys_platform: str) -> Platform:
    return Platform.from_sys_platform(sys_platform, machine="arm64", executable="/usr/bin/python3")


def test_macos_always_runs_with_the_real_helper_factory(tmp_path: Path) -> None:
    factory = helper_for(_platform("darwin"), home=tmp_path)

    assert isinstance(factory, partial)
    assert factory.func is helper_client
    assert factory.keywords == {"home": tmp_path}


def test_the_helper_client_dials_the_socket_of_the_home_it_is_given(tmp_path: Path) -> None:
    assert (
        helper_client(home=tmp_path / "one").sock_path
        != helper_client(home=tmp_path / "two").sock_path
    )


def test_linux_has_no_helper(tmp_path: Path) -> None:
    assert helper_for(_platform("linux"), home=tmp_path) is None


def test_an_unknown_platform_is_refused_not_given_a_default(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported platform"):
        _platform("freebsd")
    platform = _platform("linux")
    object.__setattr__(platform, "system", "freebsd")
    with pytest.raises(ValueError, match=r"no permissions-helper decision.*freebsd"):
        helper_for(platform, home=tmp_path)
