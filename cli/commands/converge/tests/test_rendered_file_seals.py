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
    RootAdmission,
    admitted_seals,
    read_root_admission,
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


def test_a_home_with_no_root_answers_with_no_admission(tmp_path: Path) -> None:
    assert read_root_admission(tmp_path) is None


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
    assert read_root_admission(tmp_path) is None

    fd = acquire_instance_lock(root_dir)
    try:
        assert read_root_admission(tmp_path) == RootAdmission(
            seals=((Path("/h/lgtm/native/config"), "grafana"),), liveness_known=True
        )
    finally:
        release_instance_lock(fd)
    assert read_root_admission(tmp_path) is None


# meiva:note
# An unreadable lock is the one case that must not go quiet: it is either no root (a surplus
# warning) or a running root whose drift would otherwise be swallowed (task #6403).
def test_an_unreadable_lock_still_answers_the_seals_it_can_read(tmp_path: Path) -> None:
    root_dir = run_dir(tmp_path) / "meiva-root"
    root_dir.mkdir(parents=True)
    seals = _record(
        tmp_path,
        [{"id": "grafana", "inputs": [{"path": "/h/lgtm/native/config", "digest": "a" * 64}]}],
    )
    (root_dir / "manifests.json").write_bytes(seals.read_bytes())
    # meiva:note
    # A directory where the lock file belongs opens as a directory: an OSError the probe cannot
    # read, which is exactly the unreadable case.
    (root_dir / "meiva-root.lock").mkdir()

    assert read_root_admission(tmp_path) == RootAdmission(
        seals=((Path("/h/lgtm/native/config"), "grafana"),), liveness_known=False
    )
