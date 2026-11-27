# meiva:link cli/fleet_update/docs/structure.md

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from base.host.atomic_io import write_text_atomic
from base.native_process.evidence import Digest
from cli.commands.lifecycle import root_driver

from .receipt import SUPERVISION_UNBOUND


class _ExpectedLaunch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    target: str
    selection: tuple[str, ...]
    launch_digest: Digest
    units: dict[str, Digest]


def _expectation_path(home: Path) -> Path:
    return home / "updates" / "fleet-supervision-expectation.json"


def stage_expected_launch(
    home: Path, *, target: str, selection: tuple[str, ...], manifest: Mapping[str, object]
) -> None:
    digest = manifest["launch_digest"]
    if not isinstance(digest, str):
        raise TypeError("target launch manifest omitted its digest")
    expected = _ExpectedLaunch(
        target=target,
        selection=selection,
        launch_digest=digest,
        units=root_driver.unit_digests(manifest),
    )
    path = _expectation_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, expected.model_dump_json(), mode=0o600)


def _changed_manifests(
    observed: Mapping[str, str | None], expected: Mapping[str, str]
) -> list[str]:
    different = observed.items() ^ expected.items()
    return sorted({unit_id for unit_id, _ in different})


def process_proof(
    home: Path,
    *,
    environ: Mapping[str, str],
    pid: int,
    target: str,
    selection: tuple[str, ...],
    require_preflight: bool,
) -> int:
    client = root_driver.root_client(home, environ=environ, pid=pid)
    snapshot = root_driver.root_status(client)
    if snapshot is None:
        print(f"meiva-root: no readable live process status for target {target}")
        return 1
    generation = snapshot["root"]["launch_digest"]
    print(f"meiva-root: observed launch generation {generation} for target {target}", flush=True)
    path = _expectation_path(home)
    try:
        expected = _ExpectedLaunch.model_validate_json(path.read_bytes())
    except FileNotFoundError:
        expected = None
    except ValidationError:
        print(f"meiva-root: invalid staged launch expectation for target {target}")
        return 1
    if expected is None or (expected.target, expected.selection) != (target, selection):
        print(
            f"meiva-root: no staged launch expectation for target {target} and requested selection"
        )
        if require_preflight:
            return 1
        print(SUPERVISION_UNBOUND)
        return 0
    if generation != expected.launch_digest:
        print(
            f"meiva-root: launch generation {generation} differs from expected {expected.launch_digest}"
        )
        return 1
    manifests = {unit["id"]: unit.get("manifest_digest") for unit in snapshot["units"]}
    if changed := _changed_manifests(manifests, expected.units):
        print(f"meiva-root: unit manifests differ from expected: {', '.join(changed)}")
        return 1
    unready = [
        f"{unit['id']}={unit['state']}" for unit in snapshot["units"] if unit["state"] != "running"
    ]
    if unready:
        print(f"meiva-root: target {target}, launch generation {generation}: " + ", ".join(unready))
        return 1
    print(f"FLEET_SUPERVISION={target} {generation} {len(expected.units)} running units")
    return 0
