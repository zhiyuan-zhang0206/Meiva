# meiva:note
# What Claude Code's first-run presets and workspace trust do to its two JSON files, as functions of the file's text. The shell reads the file, asks for the plan, backs the file up when the plan says so and writes the merged document.
# meiva:link meiva/shell/coding_tools/docs/coding-tools.meiva.okf.md
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, TypeGuard

# meiva:note
# Two dialogs break a non-interactive spawn on a fresh HOME: the bypass-permissions confirmation (>=2.1.274 defaults to No/exit, so a blind Enter kills the session) and the fullscreen upsell. Both answers persist in files, so presetting them is enough. `~/.claude/settings.json`: `skipDangerousModePermissionPrompt: true` ("whether the user has accepted the bypass permissions mode dialog"). `~/.claude.json`: `fullscreenUpsellSeenCount: 3`, since 2.1.278 shows the upsell while this is below its threshold of 3 (bundle: `<x8e`, x8e=3).
SETTINGS_PRESET: dict[str, object] = {"skipDangerousModePermissionPrompt": True}
CONFIG_PRESET: dict[str, object] = {"fullscreenUpsellSeenCount": 3}


# meiva:note
# What to do with one JSON file. `keep` leaves it alone, `write` publishes `data`, and `refuse` leaves it as it is, after a backup, and raises `problem`: an unparsable file is never overlaid, because that would destroy the owner's config.
@dataclass(frozen=True)
class FilePlan:
    action: Literal["keep", "write", "refuse"]
    data: dict[str, object]
    problem: str


# meiva:note
# True when `current` already carries the desired preset value. A numeric preset is a floor.
def preset_satisfied(current: object, desired: object) -> bool:
    if isinstance(desired, bool):
        return current is desired
    if isinstance(desired, int):
        return (
            isinstance(current, (int, float))
            and not isinstance(current, bool)
            and current >= desired
        )
    return current == desired


def _is_str_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def _parsed(raw: str | None, label: str) -> tuple[dict[str, object] | None, str]:
    if raw is None:
        return None, ""
    try:
        loaded: object = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        return None, (
            f"{label} is not valid JSON ({exc}); left as-is with a backup taken. "
            "Fix or remove it, then relaunch."
        )
    if not _is_str_dict(loaded):
        return None, f"{label} is not a JSON object; left as-is with a backup taken."
    return loaded, ""


# meiva:note
# Existing keys are preserved verbatim; a satisfied update makes the call a no-op; an unparsable file is refused (fail fast). `raw` is the file's text, None when the file is absent.
def plan_preset(raw: str | None, updates: Mapping[str, object], label: str) -> FilePlan:
    data, problem = _parsed(raw, label)
    if problem:
        return FilePlan("refuse", {}, problem)
    if data is not None and all(preset_satisfied(data.get(k), v) for k, v in updates.items()):
        return FilePlan("keep", data, "")
    return FilePlan("write", {**(data or {}), **updates}, "")


# meiva:note
# Mark the workspace trusted in `~/.claude.json`'s `projects` map, so Claude Code does not stop on its trust dialog. An already trusted workspace keeps the file as it is.
def plan_trust(raw: str | None, workspace_key: str, label: str) -> FilePlan:
    data, problem = _parsed(raw, label)
    if problem:
        return FilePlan("refuse", {}, problem)
    config = dict(data or {})
    raw_projects = config.get("projects")
    projects = dict(raw_projects) if _is_str_dict(raw_projects) else {}
    raw_project = projects.get(workspace_key)
    project = dict(raw_project) if _is_str_dict(raw_project) else {}
    if project.get("hasTrustDialogAccepted"):
        return FilePlan("keep", config, "")
    project["hasTrustDialogAccepted"] = True
    projects[workspace_key] = project
    config["projects"] = projects
    return FilePlan("write", config, "")
