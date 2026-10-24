# meiva:note
# Which pinned native LGTM backends a converge pass installs.
#
# The shell in `lgtm_native.py` reads the version markers a previous pass left in the native
# directory and the pins from the checkout; these functions decide which backends are already
# current and which need their archive downloaded.

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "InstallBackend",
    "InstalledMarkers",
    "KeepBackend",
    "plan_backend_installs",
    "platform_tag",
    "require_backend_selection",
    "station_backends",
]

# meiva:note
# The tag a native directory with no platform marker was installed for: the markers were added
# after the first darwin-arm64 installs, so a missing one means that platform.
_LEGACY_TAG = "darwin_arm64"


# meiva:note
# The pinned release tag for a platform, or None when native LGTM does not support it.
def platform_tag(system: str, machine: str) -> str | None:
    machine = machine.lower()
    if system == "darwin" and machine in {"arm64", "aarch64"}:
        return "darwin_arm64"
    if system == "linux" and machine in {"x86_64", "amd64"}:
        return "linux_amd64"
    return None


# meiva:note
# What a previous pass recorded for one backend: the installed version and the platform tag.
# Either is None when its marker file is absent.
@dataclass(frozen=True)
class InstalledMarkers:
    version: str | None
    platform_tag: str | None


# meiva:note
# The backend's archive must be downloaded, verified and installed.
@dataclass(frozen=True)
class InstallBackend:
    name: str


# meiva:note
# The backend is already installed at the pinned version for this platform.
@dataclass(frozen=True)
class KeepBackend:
    name: str
    version: str


# meiva:note
# Refuse an empty selection or one naming a service that is not a native LGTM backend.
def require_backend_selection(services: frozenset[str], backends: tuple[str, ...]) -> None:
    if not services or services - set(backends):
        raise ValueError("native LGTM preparation requires selected backend names")


# meiva:note
# One step per selected backend, in the pin order: keep it when both markers match the pin and
# the platform, install it otherwise.
def plan_backend_installs(
    *,
    tag: str,
    pinned_versions: Mapping[str, str],
    installed: Mapping[str, InstalledMarkers],
    services: frozenset[str],
) -> tuple[InstallBackend | KeepBackend, ...]:
    steps: list[InstallBackend | KeepBackend] = []
    for name, version in pinned_versions.items():
        if name not in services:
            continue
        markers = installed.get(name, InstalledMarkers(None, None))
        recorded_tag = markers.platform_tag if markers.platform_tag is not None else _LEGACY_TAG
        if recorded_tag == tag and markers.version == version:
            steps.append(KeepBackend(name, version))
        else:
            steps.append(InstallBackend(name))
    return tuple(steps)


# meiva:note
# The backends this converge pass prepares: the selected ones on a station, none elsewhere.
#
# Station identity is the legacy `lgtm-host` marker or the declarative `observability-station`
# capability. A home that is neither prepares nothing, even when the services are selected, and a
# station prepares only the backends the selection names.
def station_backends(
    *,
    marker_present: bool,
    roles: frozenset[str] | None,
    services: frozenset[str],
    backends: tuple[str, ...],
) -> frozenset[str]:
    declared = roles is not None and "observability-station" in roles
    if not (marker_present or declared):
        return frozenset()
    return services & set(backends)
