# meiva:note
# The four-layer cutover check for the `meiva-ops` dashboard.
#
# A dashboard can vanish while every readiness probe passes, so the check looks at four layers:
# the installed file, the hash sidecar, the dashboard Grafana serves and the folders it lists.
# The shell in `_lgtm_provisioning.py` reads the file, the sidecar and Grafana's answers; these
# functions turn what it read into the problems to print.

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from cli.commands.observability.plans.lgtm.dashboard_plan import (
    DASHBOARD_FOLDER,
    DASHBOARD_SOURCE_KEY,
    DASHBOARD_UID,
    RENDERED_DASHBOARD_REL,
)

__all__ = [
    "GrafanaReply",
    "file_problems",
    "grafana_problems",
    "sidecar_problems",
]


# meiva:note
# One Grafana answer: the HTTP status (0 when it could not be reached) and the decoded JSON body,
# or the transport error text when the status is 0.
@dataclass(frozen=True)
class GrafanaReply:
    status: int
    payload: object


def _object(value: object) -> dict[str, object]:
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def _objects(value: object) -> list[dict[str, object]]:
    items = cast("list[object]", value) if isinstance(value, list) else []
    return [cast("dict[str, object]", item) for item in items if isinstance(item, dict)]


# meiva:note
# Layer one: the installed dashboard file parses, has the fixed uid and carries panels.
#
# `document` is the parsed JSON object, empty when the file is missing or not an object.
def file_problems(label: str, document: Mapping[str, object]) -> tuple[str, ...]:
    if not document:
        return (f"dashboard file is missing or not a JSON object: {label}",)
    problems: list[str] = []
    uid = document.get("uid")
    uid = uid if isinstance(uid, str) else ""
    if uid != DASHBOARD_UID:
        problems.append(f"dashboard uid is {uid!r}, expected {DASHBOARD_UID!r}")
    panels = document.get("panels")
    if not (isinstance(panels, list) and panels):
        problems.append("dashboard carries no panels")
    return tuple(problems)


# meiva:note
# Layer two: the sidecar names the dashboard, marks a render rather than the reference and keeps
# no pre-rename `ava-` entry.
def sidecar_problems(name: str, recorded: Mapping[str, object]) -> tuple[str, ...]:
    problems: list[str] = []
    if RENDERED_DASHBOARD_REL not in recorded:
        problems.append(f"{name} does not record {RENDERED_DASHBOARD_REL}")
    if recorded.get(DASHBOARD_SOURCE_KEY) == "reference":
        problems.append("the installed dashboard is the checkout reference, not a render")
    problems.extend(
        f"{name} still records the pre-rename {key}"
        for key in sorted(recorded)
        if key.startswith("dashboards/ava-")
    )
    return tuple(problems)


def _pre_rename_folder(folder: Mapping[str, object]) -> bool:
    return any(str(folder.get(field) or "").lower().startswith("ava") for field in ("uid", "title"))


# meiva:note
# Layers three and four: Grafana serves the dashboard from the `Meiva` folder and no pre-rename
# folder survives.
#
# An unreachable Grafana is one problem and stops there, even on a host that ran `meiva lgtm
# off` while its roles still declare the station: the declaration says the host serves the
# dashboard, so a stopped backend is that claim unmet.
def grafana_problems(
    grafana_url: str, dashboard: GrafanaReply, folders: GrafanaReply
) -> tuple[str, ...]:
    if dashboard.status == 0:
        return (f"Grafana is unreachable at {grafana_url}: {dashboard.payload}",)
    problems: list[str] = []
    if dashboard.status != 200:
        problems.append(f"Grafana answered {dashboard.status} for dashboard uid {DASHBOARD_UID!r}")
    else:
        folder = _object(_object(dashboard.payload).get("meta")).get("folderTitle")
        if folder != DASHBOARD_FOLDER:
            problems.append(f"dashboard sits in folder {folder!r}, expected {DASHBOARD_FOLDER!r}")
    if folders.status != 200:
        problems.append(f"Grafana answered {folders.status} for /api/folders")
        return tuple(problems)
    problems.extend(
        f"pre-rename folder still present: {folder.get('title')!r} ({folder.get('uid')})"
        for folder in _objects(folders.payload)
        if _pre_rename_folder(folder)
    )
    return tuple(problems)
