# meiva:note
# The model tracker's persisted state: `providers -> {reported, status, announced, pending?}` in
# `state.json`. A file outside that contract is refused when it is read, not repaired.

from __future__ import annotations

import json
from pathlib import Path

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_of

__all__ = ["load_state", "save_state"]


def _valid_status(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("model tracker state status must be a string")
    if value != "ok" and (
        not value.startswith("error:") or not value.removeprefix("error:").strip()
    ):
        raise ValueError("model tracker state status must be ok or error:<message>")
    return value


def _loaded_pending(pending: object) -> dict[str, object]:
    if not is_object_dict(pending) or "status" not in pending or "rounds" not in pending:
        raise ValueError("model tracker state pending must carry status and rounds")
    rounds = pending["rounds"]
    if not isinstance(rounds, int) or rounds < 1:
        raise TypeError("model tracker state pending rounds must be a positive integer")
    return {"status": _valid_status(pending["status"]), "rounds": rounds}


def _loaded_provider(entry: dict[str, object]) -> dict[str, object]:
    if "reported" not in entry or "status" not in entry:
        raise ValueError("model tracker state provider is missing reported or status")
    reported = entry["reported"]
    if not is_object_list(reported) or not all(isinstance(model_id, str) for model_id in reported):
        raise ValueError("model tracker state reported must be a list of strings")
    status = _valid_status(entry["status"])
    # meiva:note
    # `announced` postdates the first state files: a file written before
    # the field existed baselines on the status it was left in, so the
    # upgrade itself never announces anything.
    loaded: dict[str, object] = {
        "reported": reported,
        "status": status,
        "announced": _valid_status(entry.get("announced", status)),
    }
    if "pending" in entry:
        loaded["pending"] = _loaded_pending(entry["pending"])
    return loaded


def load_state(path: Path) -> dict[str, dict[str, dict[str, object]]]:
    if not path.exists():
        return {"providers": {}}
    raw = object_of(json.loads, path.read_text())
    if not is_object_dict(raw):
        raise TypeError("model tracker state response must be a JSON object")
    raw_providers = raw.get("providers")
    if not is_object_dict(raw_providers):
        raise ValueError("model tracker state is missing providers")
    providers: dict[str, dict[str, object]] = {}
    for provider, entry in raw_providers.items():
        if not isinstance(provider, str) or not is_object_dict(entry):
            raise TypeError("model tracker state has an invalid provider entry")
        providers[provider] = _loaded_provider(entry)
    return {"providers": providers}


def save_state(path: Path, state: dict[str, dict[str, dict[str, object]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)
