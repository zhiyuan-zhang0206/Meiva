# meiva:note
# What converge does with the generated `meiva-ops` dashboard and the files around it.
#
# The dashboard is the one provisioning file converge generates rather than copies, and the one
# whose absence is invisible: Grafana serves an empty list and every readiness probe still
# passes. These functions decide from the render outcome, the installed file and the hash
# sidecar what to write, keep or refuse; the shell in `_lgtm_provisioning.py` reads and writes.

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "DASHBOARD_FOLDER",
    "DASHBOARD_SOURCE_KEY",
    "DASHBOARD_UID",
    "RENDERED_DASHBOARD_REL",
    "DashboardFacts",
    "DashboardOutcome",
    "FailedDashboardRender",
    "InstallReference",
    "KeepInstalled",
    "KeepRendered",
    "RefuseDashboard",
    "RenderedDashboard",
    "WriteRendered",
    "ghost_hash_keys",
    "hash_record_update",
    "orphan_removable",
    "plan_dashboard_fallback",
    "plan_rendered_dashboard",
]

DASHBOARD_UID = "meiva-ops-main"
RENDERED_DASHBOARD_REL = f"dashboards/{DASHBOARD_UID}.json"
DASHBOARD_SOURCE_KEY = "$dashboard-source"
DASHBOARD_FOLDER = "Meiva"


# meiva:note
# The dashboard document the metric-registry render produced and the plugins whose load failed
# (skipped, so their panels are missing from the render).
@dataclass(frozen=True)
class RenderedDashboard:
    content: str
    failed_plugins: tuple[str, ...]


# meiva:note
# The render raised; `error` is the exception text.
@dataclass(frozen=True)
class FailedDashboardRender:
    error: str


DashboardOutcome = RenderedDashboard | FailedDashboardRender


# meiva:note
# The installed render equals the new one: only the sidecar needs to say so.
@dataclass(frozen=True)
class KeepRendered:
    digest: str


# meiva:note
# The new render replaces the installed file and the sidecar records its digest.
@dataclass(frozen=True)
class WriteRendered:
    digest: str


# meiva:note
# Whether the installed file already holds the new render.
def plan_rendered_dashboard(
    *, current: str | None, rendered: str, digest: str
) -> KeepRendered | WriteRendered:
    return KeepRendered(digest) if current == rendered else WriteRendered(digest)


# meiva:note
# What a file says about being the `meiva-ops` dashboard: its uid and whether it has panels.
@dataclass(frozen=True)
class DashboardFacts:
    uid: str
    has_panels: bool

    # meiva:note
    # True for the fixed uid with at least one panel; anything else is not this dashboard.
    def is_ops_dashboard(self) -> bool:
        return self.uid == DASHBOARD_UID and self.has_panels


# meiva:note
# Keep the installed file: it is the same dashboard the failed render would have replaced.
@dataclass(frozen=True)
class KeepInstalled: ...


# meiva:note
# Install the checkout's reference copy, recorded in the sidecar as the reference.
@dataclass(frozen=True)
class InstallReference:
    digest: str


# meiva:note
# Neither the installed file nor the reference is this dashboard: converge must fail rather than
# ship zero dashboards.
@dataclass(frozen=True)
class RefuseDashboard: ...


# meiva:note
# What a failed render leaves on the station.
#
# The installed file is kept only when it is the same dashboard; a pre-rename file, a
# half-written one or none is replaced by the checkout's reference copy when that is usable.
def plan_dashboard_fallback(
    *, installed: DashboardFacts, reference_text: str, reference: DashboardFacts, digest: str
) -> KeepInstalled | InstallReference | RefuseDashboard:
    if installed.is_ops_dashboard():
        return KeepInstalled()
    if reference_text and reference.is_ops_dashboard():
        return InstallReference(digest)
    return RefuseDashboard()


# meiva:note
# The sidecar after recording `digest` for `key` with `source`, or None when it already says so.
def hash_record_update(
    recorded: Mapping[str, object], *, key: str, digest: str, source: str
) -> dict[str, object] | None:
    if recorded.get(key) == digest and recorded.get(DASHBOARD_SOURCE_KEY) == source:
        return None
    return {**recorded, key: digest, DASHBOARD_SOURCE_KEY: source}


# meiva:note
# The sidecar keys that name no file under the provisioning directory.
#
# Keys starting with `$` are metadata, not files. A rename's leftover entry cannot outlive the
# file it described and leave the cutover check permanently red.
def ghost_hash_keys(recorded: Mapping[str, object], *, present: frozenset[str]) -> tuple[str, ...]:
    return tuple(sorted(key for key in recorded if not key.startswith("$") and key not in present))


# meiva:note
# Whether a rendered file whose source template vanished is untouched derived state that can be
# removed: its content still hashes to what converge recorded. A copy the user edited is kept.
def orphan_removable(*, recorded: str | None, actual: str) -> bool:
    return recorded is not None and actual == recorded
