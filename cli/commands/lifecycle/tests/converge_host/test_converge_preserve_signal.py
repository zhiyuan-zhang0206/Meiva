from __future__ import annotations

import contextlib
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from base import db
from base.packages.extensions import materialize
from base.telemetry import converge_report
from cli.commands.converge.rendered_file import write_rendered_guarded
from cli.commands.extensions.materialize import materialize_cluster_extensions
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database


def _capture_emit(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, ...]]:
    emitted: list[tuple[object, ...]] = []

    def record_emit(*args: object, **kwargs: object) -> None:
        emitted.append((*args, kwargs))

    monkeypatch.setattr(converge_report, "emit", record_emit)
    return emitted


def test_preserve_hit_reports_converge_file_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    emitted = _capture_emit(monkeypatch)

    target = tmp_path / "rendered.yaml"
    hashes = tmp_path / "rendered-hashes.json"
    assert write_rendered_guarded(target, "v1", hashes, "k", surface="test-surface") is None

    target.write_text("hand-edited", encoding="utf-8")
    warning = write_rendered_guarded(target, "v2", hashes, "k", surface="test-surface")

    assert warning is not None and "modified locally" in warning
    assert target.read_text(encoding="utf-8") == "hand-edited"
    assert emitted == [
        (
            "telemetry",
            "converge_file_preserved",
            {
                "level": "warning",
                "source": "converge",
                "attributes": {"path": str(target), "key": "k", "surface": "test-surface"},
            },
        )
    ]


# meiva:note
# A repository-owned surface is restored and the drift is still reported: the
# render wins, and the fact that someone edited it does not go silent. The
# record follows the restore, so the next converge is a clean write.
# meiva:link cli/commands/converge/docs/rendered-files.meiva.okf.md
def test_overwrite_policy_restores_the_render_and_reports_the_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    emitted = _capture_emit(monkeypatch)

    target = tmp_path / "rendered.yaml"
    hashes = tmp_path / "rendered-hashes.json"
    assert write_rendered_guarded(target, "v1", hashes, "k", surface="test-surface") is None

    target.write_text("hand-edited", encoding="utf-8")
    message = write_rendered_guarded(
        target, "v2", hashes, "k", surface="test-surface", on_local_edit="overwrite"
    )

    assert message is not None and "overwritten from the rendered source" in message
    assert target.read_text(encoding="utf-8") == "v2"
    assert emitted == [
        (
            "telemetry",
            "converge_file_overwritten",
            {
                "level": "warning",
                "source": "converge",
                "attributes": {"path": str(target), "key": "k", "surface": "test-surface"},
            },
        )
    ]
    assert (
        write_rendered_guarded(
            target, "v2", hashes, "k", surface="test-surface", on_local_edit="overwrite"
        )
        is None
    )


class _PoolStub:
    def __enter__(self) -> _PoolStub:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def connection(self) -> contextlib.AbstractContextManager[object]:
        return contextlib.nullcontext(object())


def test_extensions_kept_local_edits_report_converge_file_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operator_database: Callable[[], Any]
) -> None:
    emitted = _capture_emit(monkeypatch)

    def fake_pool(_database: db.Database) -> _PoolStub:
        return _PoolStub()

    monkeypatch.setattr(db.Database, "pool", fake_pool)

    def kept_edit(_conn: object, *, dest_root: Path) -> SimpleNamespace:
        assert dest_root == tmp_path / "skills"
        return SimpleNamespace(
            landed=[], updated=[], kept_local_edits=["edit-demo"], missing_blob=[]
        )

    monkeypatch.setattr(materialize, "materialize_skills", kept_edit)

    materialize_cluster_extensions(
        database_factory=operator_database, skills_root=tmp_path / "skills"
    )

    assert emitted == [
        (
            "telemetry",
            "converge_file_preserved",
            {
                "level": "warning",
                "source": "converge",
                "attributes": {
                    "path": str(tmp_path / "skills" / "edit-demo"),
                    "key": "edit-demo",
                    "surface": "extensions",
                },
            },
        )
    ]
