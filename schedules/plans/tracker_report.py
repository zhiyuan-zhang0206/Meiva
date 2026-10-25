# meiva:note
# What the model update tracker tells the P0 lead, from the checker's exit status and report.
#
# `check_model_updates.py` exits 0 when nothing is new, 2 when it found candidates (and wrote
# `last-report.json`), and anything else on a failure. The report is validated as it is
# rendered: a malformed one raises instead of being summarised into a plausible message.
# meiva:link schedules/README.md

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypeGuard

__all__ = [
    "Failed",
    "Quiet",
    "ReadCandidates",
    "TrackerVerdict",
    "candidate_message",
    "failure_message",
    "plan_tracker_exit",
]


@dataclass(frozen=True)
class Quiet:
    pass


@dataclass(frozen=True)
class ReadCandidates:
    pass


@dataclass(frozen=True)
class Failed:
    message: str


TrackerVerdict = Quiet | ReadCandidates | Failed


_DETAIL_CHARS = 1000


def failure_message(detail: str) -> str:
    return (
        f"Model update tracker failed:\n{detail[-_DETAIL_CHARS:]}\n"
        "Check the schedule log and report directory."
    )


# meiva:note
# `output` is the checker's stdout and stderr joined; only its tail is reported, and the exit
# code line stays whole: the message keeps 1000 characters of detail, and a long output used to
# push the exit code out of them.
def plan_tracker_exit(returncode: int, output: str) -> TrackerVerdict:
    if returncode == 0:
        return Quiet()
    if returncode == 2:
        return ReadCandidates()
    head = f"exit code {returncode}\n"
    return Failed(failure_message(head + output[-(_DETAIL_CHARS - len(head)) :]))


def _is_raw_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _is_str_dict(value: object) -> TypeGuard[dict[str, object]]:
    return _is_raw_dict(value) and all(isinstance(key, str) for key in value)


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_str_list(value: object) -> TypeGuard[list[str]]:
    return _is_object_list(value) and all(isinstance(item, str) for item in value)


def candidate_message(report: object, report_file: str) -> str:
    payload = _object(report, "model update tracker JSON report")
    actionable = _object(payload["actionable_candidates"], "report actionable_candidates")
    providers = _object(payload["providers"], "report providers")
    lines = ["New upstream model candidates:"]
    for provider, model_ids in actionable.items():
        if not _is_object_list(model_ids):
            raise TypeError("report actionable candidate has an invalid shape")
        provider_report = _object(providers[provider], "report provider result")
        series_models = _object(provider_report["series_models"], "report series_models")
        for model_id in model_ids:
            if not isinstance(model_id, str):
                raise TypeError("report candidate id must be a string")
            line = f"- {provider}: {model_id}"
            known = series_models[model_id]
            if not _is_str_list(known):
                raise ValueError("report same-series ids must be strings")
            if known:
                joined = ", ".join(known)
                line += f" (registry already binds {joined} in this series; review supersession)"
            lines.append(line)
    status_changes = payload["status_changes"]
    if not _is_str_list(status_changes):
        raise ValueError("report status_changes must be a list of strings")
    if status_changes:
        lines.append(f"Provider status changed: {', '.join(status_changes)}")
    lines.append(f"Full report: {report_file}")
    return "\n".join(lines)


def _object(value: object, what: str) -> Mapping[str, object]:
    if not _is_str_dict(value):
        raise TypeError(f"{what} must be an object")
    return value
