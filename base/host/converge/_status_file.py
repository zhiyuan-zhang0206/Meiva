"""Shared JSON and file framing for converge statuses."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of


def to_json(state: str, diagnostic: str) -> str:
    return json.dumps({"state": state, "diagnostic": diagnostic})


def from_json[State, Status](
    text: str, state_type: Callable[[str], State], status_type: Callable[[State, str], Status]
) -> Status:
    data = object_of(json.loads, text)
    if not is_object_dict(data):
        raise TypeError("a converge status file must hold a JSON object")
    state, diagnostic = data["state"], data["diagnostic"]
    if not isinstance(state, str) or not isinstance(diagnostic, str):
        raise TypeError("a converge status file needs a string state and diagnostic")
    return status_type(state_type(state), diagnostic)


def from_file[Status](path: Path, decode: Callable[[str], Status]) -> Status | None:
    if not path.exists():
        return None
    try:
        return decode(path.read_text())
    except (ValueError, KeyError, TypeError):
        # A truncated write (JSONDecodeError), unknown state value or obsolete shape (missing
        # key / non-object JSON) is replaced by the next converge.
        return None


def write_status(path: Path, text: str) -> None:
    path.write_text(text)
