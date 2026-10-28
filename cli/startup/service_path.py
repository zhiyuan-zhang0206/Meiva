# meiva:note
# The caller's host facts `meiva init` reads, and the tool directories a new home declares.

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

__all__ = ["BirthFacts", "CallerHost", "ServicePathChoice", "choose_service_path"]


# meiva:note
# What a home's birth needs that only the process entry can choose: the instant it is born and
# the secrets minted for a gateway unit (the cluster's human secret unless the operator gave
# one, the two Redis logins, and the logical-backup passphrase). The entry mints them once per
# `meiva init`; they stay out of every repr.
@dataclass(frozen=True)
class BirthFacts:
    now: datetime
    cluster_secret: str = field(repr=False)
    redis_admin_password: str = field(repr=False)
    redis_password: str = field(repr=False)
    backup_passphrase: str = field(repr=False)


# meiva:note
# What the process that runs `meiva init` says about its host.
#
# The entry reads these once; nothing below it reads the environment or probes a port.
# `occupied_ports` are those of the fixed port table that something already holds.
# `local_names` are the host names and interface addresses by which this host is known, in
# lowercase; a remote data-plane config that names one of them is refused. `birth` is what the
# home's birth takes from the entry.
# `user_home` is the operator's own home, the one a `~` on the command line names; the entry reads
# it once, and a path a person typed expands against it rather than against the process's user
# database.
# `transport_key` is the key the operator exported to open a database-capability bundle, already
# taken out of the environment (empty when none was exported); only a join that carries a
# bundle reads it. It is a secret, so it stays out of the repr.
@dataclass(frozen=True)
class CallerHost:
    service_path: str | None
    path: str
    virtual_env: str | None
    prefix_venv: str | None
    occupied_ports: frozenset[int]
    local_names: frozenset[str]
    birth: BirthFacts
    user_home: Path
    transport_key: str = field(default="", repr=False)


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
