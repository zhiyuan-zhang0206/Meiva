# meiva:note
# Which pinned native LGTM backends a converge pass installs, where an archive's contents go and
# how the pinned Loki parser is asked to check the config, decided from values alone.

from __future__ import annotations

from typing import Literal, cast

import pytest

from cli.commands.observability.plans.lgtm.archive import (
    ExtractMember,
    ExtractTree,
    checksum_problem,
    extraction_plan,
    install_markers,
    loki_verify_argv,
    loki_verify_error,
)
from cli.commands.observability.plans.lgtm.install import (
    InstallBackend,
    InstalledMarkers,
    KeepBackend,
    plan_backend_installs,
    platform_tag,
    require_backend_selection,
    station_backends,
)

BACKENDS = ("loki", "prometheus", "grafana")
PINS = {"loki": "3.7.6", "prometheus": "3.13.2", "grafana": "13.2.3"}


@pytest.mark.parametrize(
    ("system", "machine", "tag"),
    [
        ("darwin", "arm64", "darwin_arm64"),
        ("darwin", "aarch64", "darwin_arm64"),
        ("linux", "x86_64", "linux_amd64"),
        ("linux", "AMD64", "linux_amd64"),
        ("linux", "arm64", None),
        ("darwin", "x86_64", None),
    ],
)
def test_only_darwin_arm64_and_linux_amd64_have_pinned_assets(
    system: Literal["darwin", "linux"], machine: str, tag: str | None
) -> None:
    assert platform_tag(system, machine) == tag


@pytest.mark.parametrize("machine", ["arm64", "x86_64", "riscv64"])
def test_an_unknown_system_is_refused_whatever_the_machine(machine: str) -> None:
    system = cast("Literal['darwin', 'linux']", "Darwin")

    with pytest.raises(AssertionError):
        platform_tag(system, machine)


def current(tag: str) -> dict[str, InstalledMarkers]:
    return {name: InstalledMarkers(version, tag) for name, version in PINS.items()}


def test_backends_installed_at_the_pinned_version_for_this_platform_are_kept() -> None:
    plan = plan_backend_installs(
        tag="darwin_arm64",
        pinned_versions=PINS,
        installed=current("darwin_arm64"),
        services=frozenset(BACKENDS),
    )

    assert plan == (
        KeepBackend("loki", "3.7.6"),
        KeepBackend("prometheus", "3.13.2"),
        KeepBackend("grafana", "13.2.3"),
    )


def test_a_stale_version_marker_installs_only_that_backend() -> None:
    installed = current("darwin_arm64") | {"loki": InstalledMarkers("old", "darwin_arm64")}

    plan = plan_backend_installs(
        tag="darwin_arm64",
        pinned_versions=PINS,
        installed=installed,
        services=frozenset(BACKENDS),
    )

    assert plan == (
        InstallBackend("loki"),
        KeepBackend("prometheus", "3.13.2"),
        KeepBackend("grafana", "13.2.3"),
    )


def test_matching_versions_from_another_platform_are_installed_again() -> None:
    plan = plan_backend_installs(
        tag="linux_amd64",
        pinned_versions=PINS,
        installed=current("darwin_arm64"),
        services=frozenset(BACKENDS),
    )

    assert plan == tuple(InstallBackend(name) for name in BACKENDS)


def test_a_backend_with_no_markers_is_installed() -> None:
    plan = plan_backend_installs(
        tag="darwin_arm64", pinned_versions=PINS, installed={}, services=frozenset(BACKENDS)
    )

    assert plan == tuple(InstallBackend(name) for name in BACKENDS)


def test_a_directory_with_no_platform_marker_was_installed_for_darwin_arm64() -> None:
    installed = {name: InstalledMarkers(version, None) for name, version in PINS.items()}

    on_darwin = plan_backend_installs(
        tag="darwin_arm64", pinned_versions=PINS, installed=installed, services=frozenset(BACKENDS)
    )
    on_linux = plan_backend_installs(
        tag="linux_amd64", pinned_versions=PINS, installed=installed, services=frozenset(BACKENDS)
    )

    assert all(isinstance(step, KeepBackend) for step in on_darwin)
    assert all(isinstance(step, InstallBackend) for step in on_linux)


def test_only_the_selected_backends_are_planned() -> None:
    plan = plan_backend_installs(
        tag="darwin_arm64", pinned_versions=PINS, installed={}, services=frozenset({"prometheus"})
    )

    assert plan == (InstallBackend("prometheus"),)


@pytest.mark.parametrize("services", [frozenset[str](), frozenset({"loki", "tempo"})])
def test_a_selection_that_is_empty_or_names_a_non_backend_is_refused(
    services: frozenset[str],
) -> None:
    with pytest.raises(ValueError, match="selected backend names"):
        require_backend_selection(services, BACKENDS)


def test_a_selection_of_backends_is_accepted() -> None:
    require_backend_selection(frozenset({"loki", "grafana"}), BACKENDS)


def test_an_executable_backend_installs_its_archive_member_at_its_binary_path() -> None:
    assert extraction_plan(
        archive_member="loki-darwin-arm64", asset_member="loki-linux-amd64", binary_path="bin/loki"
    ) == ExtractMember("loki-linux-amd64", "bin/loki")


def test_the_constant_member_is_used_when_the_pin_names_none() -> None:
    assert extraction_plan(
        archive_member="loki-darwin-arm64", asset_member="", binary_path="bin/loki"
    ) == ExtractMember("loki-darwin-arm64", "bin/loki")


def test_a_release_tree_backend_installs_its_top_level_directory() -> None:
    assert extraction_plan(
        archive_member=None, asset_member="", binary_path="grafana-home/bin/grafana"
    ) == ExtractTree("grafana-home")


def test_a_finished_install_leaves_a_version_and_a_platform_marker() -> None:
    assert install_markers("loki", version="3.7.6", tag="linux_amd64") == (
        ("version-loki", "3.7.6\n"),
        ("platform-loki", "linux_amd64\n"),
    )


def test_a_matching_checksum_has_no_problem() -> None:
    assert checksum_problem("loki", "3.7.6", actual="ab", expected="ab") is None


def test_a_checksum_mismatch_refuses_to_install() -> None:
    assert checksum_problem("loki", "3.7.6", actual="ab", expected="cd") == (
        "native LGTM loki 3.7.6 SHA256 mismatch: got ab, expected cd — refusing to install"
    )


def test_the_loki_parser_is_asked_to_verify_the_rendered_config() -> None:
    assert loki_verify_argv("/h/lgtm/native/bin/loki", "/h") == (
        "/h/lgtm/native/bin/loki",
        "-config.file=/h/lgtm/native/config/loki.yaml",
        "-verify-config",
    )


def test_a_rejected_config_surfaces_the_parser_complaint_without_trailing_whitespace() -> None:
    assert loki_verify_error("unknown config field\n") == (
        "Loki config verification failed: unknown config field"
    )


@pytest.mark.parametrize(
    ("marker_present", "roles"),
    [(True, frozenset({"gateway"})), (False, frozenset({"observability-station"})), (True, None)],
    ids=["lgtm-host-marker", "station-capability", "marker-before-roles"],
)
def test_a_marked_or_station_home_prepares_the_selected_backends(
    marker_present: bool, roles: frozenset[str] | None
) -> None:
    assert station_backends(
        marker_present=marker_present,
        roles=roles,
        services=frozenset({"loki", "prometheus", "otel-collector"}),
        backends=BACKENDS,
    ) == frozenset({"loki", "prometheus"})


@pytest.mark.parametrize("roles", [None, frozenset({"gateway", "agent-runner"})])
def test_a_home_that_is_not_the_station_prepares_nothing_even_when_selected(
    roles: frozenset[str] | None,
) -> None:
    assert (
        station_backends(
            marker_present=False, roles=roles, services=frozenset(BACKENDS), backends=BACKENDS
        )
        == frozenset()
    )


def test_a_station_with_no_backend_selected_prepares_nothing() -> None:
    assert (
        station_backends(
            marker_present=True,
            roles=None,
            services=frozenset({"otel-collector"}),
            backends=BACKENDS,
        )
        == frozenset()
    )
