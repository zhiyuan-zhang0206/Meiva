# meiva:note
# The admission record a root writes at start is the only place that says which paths are sealed
# and by which unit; converge reads it to decide whether the file it just wrote is one of them
# (task #6403). A malformed or absent record answers with nothing rather than failing the render.
from __future__ import annotations

import json
from pathlib import Path

from base.native_process.root_control.instance_lock import (
    acquire_instance_lock,
    release_instance_lock,
)
from base.paths import run_dir
from cli.commands.converge.rendered_file import (
    admitted_seals,
    live_root_seals,
    sealed_units,
)


def _record(tmp_path: Path, units: list[dict[str, object]]) -> Path:
    path = tmp_path / "manifests.json"
    path.write_text(json.dumps({"units": units}), encoding="utf-8")
    return path


def test_the_record_names_every_seal_and_its_unit(tmp_path: Path) -> None:
    path = _record(
        tmp_path,
        [
            {"id": "grafana", "inputs": [{"path": "/h/lgtm/native/config", "digest": "a" * 64}]},
            {"id": "gateway", "inputs": []},
            {
                "id": "otel-collector",
                "inputs": [{"path": "/h/otel/config.yaml", "digest": "b" * 64}],
            },
        ],
    )
    assert admitted_seals(path) == (
        (Path("/h/lgtm/native/config"), "grafana"),
        (Path("/h/otel/config.yaml"), "otel-collector"),
    )


def test_a_malformed_record_is_no_record(tmp_path: Path) -> None:
    assert admitted_seals(tmp_path / "missing.json") == ()
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert admitted_seals(broken) == ()


def test_a_file_beneath_a_sealed_directory_belongs_to_its_unit() -> None:
    seals = [(Path("/h/lgtm/native/config"), "grafana"), (Path("/h/otel/config.yaml"), "otel")]
    assert sealed_units(Path("/h/lgtm/native/config/provisioning/rules.yml"), seals) == ("grafana",)
    assert sealed_units(Path("/h/lgtm/native/config"), seals) == ("grafana",)
    assert sealed_units(Path("/h/otel/config.yaml"), seals) == ("otel",)
    assert sealed_units(Path("/h/lgtm/native/configs/other"), seals) == ()
    assert sealed_units(Path("/h/elsewhere"), seals) == ()


def test_a_home_with_no_root_answers_with_no_seals(tmp_path: Path) -> None:
    assert live_root_seals(tmp_path) == ()


# meiva:note
# Liveness is the instance lock the root holds for its whole life, read without a request: a
# converge pass must not reach the service tree it runs next to (task #6403).
def test_a_held_instance_lock_makes_the_record_live(tmp_path: Path) -> None:
    root_dir = run_dir(tmp_path) / "meiva-root"
    root_dir.mkdir(parents=True)
    seals = _record(
        tmp_path,
        [{"id": "grafana", "inputs": [{"path": "/h/lgtm/native/config", "digest": "a" * 64}]}],
    )
    (root_dir / "manifests.json").write_bytes(seals.read_bytes())
    assert live_root_seals(tmp_path) == ()

    fd = acquire_instance_lock(root_dir)
    try:
        assert live_root_seals(tmp_path) == ((Path("/h/lgtm/native/config"), "grafana"),)
    finally:
        release_instance_lock(fd)
    assert live_root_seals(tmp_path) == ()
