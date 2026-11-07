from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter

from base.host.atomic_io import write_text_atomic
from base.host.boundary.json_values import is_object_dict, is_object_list
from base.telemetry.converge_report import report_converge_overwrite, report_converge_preserve
from cli.boundary.json_body import loads

LocalEditPolicy = Literal["preserve", "overwrite"]
_HASHES: TypeAdapter[dict[str, str]] = TypeAdapter(dict[str, str])


# meiva:note
# The sidecar records the sha256 of what converge last wrote; a destination whose
# hash differs from it changed outside converge. `on_local_edit` names the owner:
# `preserve` keeps a surface a person may own and leaves the record untouched,
# `overwrite` restores a repository-owned surface and moves the record with it.
# A destination with no record is adopted under either policy, and either hit
# reports one telemetry event through converge_report.
#
# The content is published atomically and owner-only from creation (the temporary file is created
# 0600 and the rename publishes only the fully written file), so a reader such as Grafana's
# provisioning watcher never sees a torn render and a config carrying a bearer is never exposed
# through the process umask.
# meiva:link cli/commands/converge/docs/rendered-files.meiva.okf.md
def write_rendered_guarded(
    path: Path,
    content: str,
    hashes_path: Path,
    key: str,
    *,
    surface: str,
    on_local_edit: LocalEditPolicy = "preserve",
) -> str | None:
    recorded: dict[str, str] = {}
    if hashes_path.exists():
        recorded = _HASHES.validate_json(hashes_path.read_text(encoding="utf-8"))
    drift: str | None = None
    if path.exists() and key in recorded:
        current = hashlib.sha256(path.read_bytes()).hexdigest()
        if current != recorded[key]:
            if on_local_edit == "preserve":
                report_converge_preserve(path=str(path), key=key, surface=surface)
                return (
                    f"{path} was modified locally; not overwritten "
                    f"(remove it and re-run converge to restore from the rendered output)"
                )
            report_converge_overwrite(path=str(path), key=key, surface=surface)
            drift = (
                f"{path} was modified locally; overwritten from the rendered source "
                "(this surface is repository-owned)"
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, content, encoding="utf-8", suffix="")
    recorded[key] = hashlib.sha256(content.encode("utf-8")).hexdigest()
    hashes_path.write_text(json.dumps(recorded, indent=2) + "\n", encoding="utf-8")
    return drift


# meiva:note
# The units whose sealed path covers `path` — the path itself or anything beneath it. A sealed
# file and a sealed directory are the two shapes the root admits.
def sealed_units(path: Path, seals: Iterable[tuple[Path, str]]) -> tuple[str, ...]:
    return tuple(unit for sealed, unit in seals if path == sealed or sealed in path.parents)


# meiva:note
# The sealed paths of the units a live root owns, or none when no root answers: a home whose root
# is down re-admits every input when it next starts, so a warning there would be noise.
def live_root_seals(home: Path) -> tuple[tuple[Path, str], ...]:
    from base.native_process.root_control.client import RootClient, RootClientError

    socket = home / "run/meiva-root/meiva-root.sock"
    if not socket.exists():
        return ()
    try:
        _ = RootClient(socket, timeout=2.0).status()
    except RootClientError:
        return ()
    return admitted_seals(home / "run/meiva-root/manifests.json")


# meiva:note
# The admission record a root writes at start is the only place that says which paths it sealed
# and for which unit. A missing or malformed record answers with nothing rather than failing a
# render.
def admitted_seals(record: Path) -> tuple[tuple[Path, str], ...]:
    try:
        raw = loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    if not is_object_dict(raw):
        return ()
    units = raw.get("units")
    if not is_object_list(units):
        return ()
    seals: list[tuple[Path, str]] = []
    for unit in units:
        if not is_object_dict(unit):
            continue
        unit_id, inputs = unit.get("id"), unit.get("inputs")
        if not isinstance(unit_id, str) or not is_object_list(inputs):
            continue
        for seal in inputs:
            if not is_object_dict(seal):
                continue
            sealed = seal.get("path")
            if isinstance(sealed, str):
                seals.append((Path(sealed), unit_id))
    return tuple(seals)


# meiva:note
# A rendered file that is also a sealed input of a running unit is the moment that unit stops
# being replaceable: the root refuses every later birth of a unit whose input changed since its
# admission, the health monitor's own replacement included (task #6403). Say it here, where the
# change is made, and name the recovery — a report one health round later reaches whoever is on
# duty, never the person who just ran converge.
def warn_sealed_inputs(home: Path, changed: Iterable[Path]) -> None:
    seals = live_root_seals(home)
    if not seals:
        return
    units = sorted({unit for path in changed for unit in sealed_units(path, seals)})
    if not units:
        return
    joined = ", ".join(units)
    from base.log import logger

    logger.warning(
        "converge wrote a sealed input of {units}: this root generation can no longer spawn them "
        "— re-admit the inputs or restart the host before they are needed",
        event="converge_sealed_input_changed",
        units=joined,
        paths=", ".join(str(path) for path in changed),
    )
    print(
        f"  ! converge changed a sealed input of {joined} — this root generation can no longer "
        "spawn them; restart the host (or re-admit the inputs) before they are needed",
        file=sys.stderr,
    )
