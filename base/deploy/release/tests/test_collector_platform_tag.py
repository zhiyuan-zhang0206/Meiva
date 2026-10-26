from __future__ import annotations

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
    system: str, machine: str, tag: str
) -> None:
    assert platform_tag_for(system, machine) == tag


@pytest.mark.parametrize(
    ("system", "machine"),
    [
        ("linux", "i686"),
        ("linux", "riscv64"),
        ("darwin", "ppc"),
        ("freebsd", "x86_64"),
        ("", "arm64"),
    ],
)
def test_a_platform_without_a_pinned_binary_has_no_tag(system: str, machine: str) -> None:
    assert platform_tag_for(system, machine) is None
