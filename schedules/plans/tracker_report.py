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
from typing import cast

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


def candidate_message(report: object, report_file: str) -> str:
    payload = _object(report, "model update tracker JSON report")
    actionable = _object(payload["actionable_candidates"], "report actionable_candidates")
    providers = _object(payload["providers"], "report providers")
    lines = ["New upstream model candidates:"]
    for provider, model_ids in actionable.items():
        if not isinstance(model_ids, list):
            raise TypeError("report actionable candidate has an invalid shape")
        provider_report = _object(providers[provider], "report provider result")
        series_models = _object(provider_report["series_models"], "report series_models")
        for model_id in cast(list[object], model_ids):
            if not isinstance(model_id, str):
                raise TypeError("report candidate id must be a string")
            line = f"- {provider}: {model_id}"
            known = series_models[model_id]
            if not isinstance(known, list) or not all(
                isinstance(value, str) for value in cast(list[object], known)
            ):
                raise ValueError("report same-series ids must be strings")
            if known:
                joined = ", ".join(cast(list[str], known))
                line += f" (registry already binds {joined} in this series; review supersession)"
            lines.append(line)
    status_changes = payload["status_changes"]
    if not isinstance(status_changes, list) or not all(
        isinstance(value, str) for value in cast(list[object], status_changes)
    ):
        raise ValueError("report status_changes must be a list of strings")
    if status_changes:
        lines.append(f"Provider status changed: {', '.join(cast(list[str], status_changes))}")
    lines.append(f"Full report: {report_file}")
    return "\n".join(lines)


def _object(value: object, what: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise TypeError(f"{what} must be an object")
    return cast(Mapping[str, object], value)
