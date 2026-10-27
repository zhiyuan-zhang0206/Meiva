from __future__ import annotations

from typing import Literal, cast

import pytest

from base.deploy.release.collector_artifact import platform_tag_for


@pytest.mark.parametrize(
    ("system", "machine", "tag"),
    [
        ("darwin", "arm64", "darwin_arm64"),
        ("darwin", "x86_64", "darwin_amd64"),
        ("linux", "x86_64", "linux_amd64"),
        ("linux", "aarch64", "linux_arm64"),
        ("linux", "arm64", "linux_arm64"),
        ("linux", "AMD64", "linux_amd64"),
    ],
)
def test_each_supported_platform_has_its_release_asset_tag(
    system: Literal["darwin", "linux"], machine: str, tag: str
) -> None:
    assert platform_tag_for(system, machine) == tag


@pytest.mark.parametrize(
    ("system", "machine"),
    [("linux", "i686"), ("linux", "riscv64"), ("darwin", "ppc"), ("darwin", "")],
)
def test_a_platform_without_a_pinned_binary_has_no_tag(
    system: Literal["darwin", "linux"], machine: str
) -> None:
    assert platform_tag_for(system, machine) is None


@pytest.mark.parametrize("machine", ["arm64", "x86_64", "riscv64", ""])
def test_an_unknown_system_is_refused_whatever_the_machine(machine: str) -> None:
    system = cast("Literal['darwin', 'linux']", "Darwin")

    with pytest.raises(AssertionError):
        platform_tag_for(system, machine)
