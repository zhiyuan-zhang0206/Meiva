# meiva:note
# The caller's host facts `meiva init` reads, and the tool directories a new home declares.

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = ["CallerHost", "ServicePathChoice", "choose_service_path"]


# meiva:note
# What the process that runs `meiva init` says about its host.
#
# The entry reads these once; nothing below it reads the environment or probes a port.
# `occupied_ports` are those of the fixed port table that something already holds.
@dataclass(frozen=True)
class CallerHost:
    service_path: str | None
    path: str
    virtual_env: str | None
    prefix_venv: str | None
    occupied_ports: frozenset[int]


# meiva:note
# The raw PATH to admit and the virtualenv `bin` directories to drop from it.
@dataclass(frozen=True)
class ServicePathChoice:
    raw: str
    excluded: tuple[str, ...]


# meiva:note
# The tool directories a home declares, from the first source that names them.
#
# A declaration the first-start inputs or the home's `.env` already carry wins and is
# admitted as written. A home with an `.env` and no declaration refuses: it never
# silently recaptures its caller's PATH. A new home takes the caller's declared
# `MEIVA_SERVICE_PATH`, else the caller's PATH without the checkout's virtualenv,
# the interpreter's own and the activated one.
def choose_service_path(
    declared: str | None, *, home_has_env: bool, caller: CallerHost, checkout: str
) -> ServicePathChoice:
    if declared is not None:
        return ServicePathChoice(declared, ())
    if home_has_env:
        raise ValueError("existing home requires an explicit MEIVA_SERVICE_PATH declaration")
    if caller.service_path is not None:
        return ServicePathChoice(caller.service_path, ())
    venvs = [Path(checkout) / ".venv"]
    if caller.prefix_venv is not None:
        venvs.append(Path(caller.prefix_venv))
    if caller.virtual_env:
        venvs.append(Path(caller.virtual_env))
    return ServicePathChoice(caller.path, tuple(str(venv / "bin") for venv in venvs))
