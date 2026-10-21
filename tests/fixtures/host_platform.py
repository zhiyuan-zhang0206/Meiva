# meiva:note
# `requires_host(...)`: a test whose subject is one operating system.
#
# Most tests belong to the `Host` port: they build the host they want and assert the same
# result on every machine. A few subjects cannot — launchd, TCC, codesign and swiftc exist
# on macOS and nowhere else — so those tests name the host they need and this plugin skips
# them everywhere else, at collection time, with one consistent reason. `--collect-only`
# still lists the test, which is what a CI lane gate compares between Linux and macOS.
# meiva:link scripts/lint/docs/host-platform-marks.meiva.okf.md

from __future__ import annotations

import pytest


# meiva:note
# Two markers on one item, or an argument `Platform` refuses, must fail collection: a skip
# that never matches hides the test on every host.
def required_host(item: pytest.Item) -> str | None:
    marks = list(item.iter_markers("requires_host"))
    if not marks:
        return None
    if len(marks) > 1:
        raise pytest.UsageError(
            f"{item.nodeid}: two @pytest.mark.requires_host markers; a test needs one host"
        )
    mark = marks[0]
    if len(mark.args) != 1 or mark.kwargs:
        raise pytest.UsageError(
            f"{item.nodeid}: @pytest.mark.requires_host takes exactly one system name, "
            'e.g. @pytest.mark.requires_host("darwin")'
        )
    return _system(mark.args[0], item.nodeid)


def _system(system: object, nodeid: str) -> str:
    from base.native_process.os_platform import Platform

    if not isinstance(system, str):
        raise pytest.UsageError(
            f"{nodeid}: @pytest.mark.requires_host takes a system name, not {system!r}"
        )
    try:
        return Platform.from_sys_platform(system, machine="unused", executable="unused").system
    except ValueError as error:
        raise pytest.UsageError(f"{nodeid}: {error}") from None


def _current_system() -> str:
    # meiva:note
    # host-ok: this module IS the translation the marker is checked against, so asking
    # the host it runs on is its subject.
    from base.native_process.os_platform import Platform

    return Platform.current().system


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    current = _current_system()
    for item in items:
        wanted = required_host(item)
        if wanted is None or wanted == current:
            continue
        item.add_marker(
            pytest.mark.skipif(
                current != wanted,
                reason=f"requires a {wanted} host: this one is {current}",
            )
        )
