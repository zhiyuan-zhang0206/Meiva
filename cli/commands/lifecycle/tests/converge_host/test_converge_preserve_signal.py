# meiva:note
# A converge that keeps local content, or restores it over a local edit, says so on the event stream.

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

import psycopg

from base import db
from base.db.code_version_gate import ProcessDbGate
from base.packages.extensions import materialize as mat
from base.packages.extensions import registry as reg
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from cli.commands.converge.rendered_file import write_rendered_guarded
from cli.commands.extensions.materialize import materialize_cluster_extensions
from cli.database.tests.cli_tests import as_machine as as_machine
from cli.database.tests.cli_tests import operator_database as operator_database

_SKILL = """---
name: edit-demo
description: a test skill, use when exercising the preserve signal
---

# edit-demo

{body}
"""


def test_preserve_hit_reports_converge_file_preserved(
    tmp_path: Path, emitted_events: EmittedEvents
) -> None:
    target = tmp_path / "rendered.yaml"
    hashes = tmp_path / "rendered-hashes.json"
    assert write_rendered_guarded(target, "v1", hashes, "k", surface="test-surface") is None

    target.write_text("hand-edited", encoding="utf-8")
    warning = write_rendered_guarded(target, "v2", hashes, "k", surface="test-surface")

    assert warning is not None and "modified locally" in warning
    assert target.read_text(encoding="utf-8") == "hand-edited"
    [event] = emitted_events.named("converge_file_preserved")
    assert (event.level, event.source) == ("warning", "converge")
    assert event.attributes == {"path": str(target), "key": "k", "surface": "test-surface"}
    assert emitted_events.names() == ["converge_file_preserved"]


# meiva:note
# A repository-owned surface is restored and the drift is still reported: the
# render wins, and the fact that someone edited it does not go silent. The
# record follows the restore, so the next converge is a clean write.
# meiva:link cli/commands/converge/docs/rendered-files.meiva.okf.md
def test_overwrite_policy_restores_the_render_and_reports_the_drift(
    tmp_path: Path, emitted_events: EmittedEvents
) -> None:
    target = tmp_path / "rendered.yaml"
    hashes = tmp_path / "rendered-hashes.json"
    assert write_rendered_guarded(target, "v1", hashes, "k", surface="test-surface") is None

    target.write_text("hand-edited", encoding="utf-8")
    message = write_rendered_guarded(
        target, "v2", hashes, "k", surface="test-surface", on_local_edit="overwrite"
    )

    assert message is not None and "overwritten from the rendered source" in message
    assert target.read_text(encoding="utf-8") == "v2"
    [event] = emitted_events.named("converge_file_overwritten")
    assert (event.level, event.source) == ("warning", "converge")
    assert event.attributes == {"path": str(target), "key": "k", "surface": "test-surface"}
    assert (
        write_rendered_guarded(
            target, "v2", hashes, "k", surface="test-surface", on_local_edit="overwrite"
        )
        is None
    )
    assert emitted_events.names() == ["converge_file_overwritten"]


# meiva:note
# A skill this machine edited, then the cluster moved on: the edit is kept and reported.
def test_extensions_kept_local_edits_report_converge_file_preserved(
    tmp_path: Path,
    as_machine: Callable[[Path], Any],
    db_conn: psycopg.Connection,
    database_gate: ProcessDbGate,
    operator_database: Callable[[], Any],
    emitted_events: EmittedEvents,
) -> None:
    home = tmp_path / "home"
    skills_root = home / "skills"
    version_one = tmp_path / "src" / "one" / "edit-demo"
    version_two = tmp_path / "src" / "two" / "edit-demo"
    for source, body in ((version_one, "Version one."), (version_two, "Version two.")):
        source.mkdir(parents=True)
        (source / "SKILL.md").write_text(_SKILL.format(body=body), encoding="utf-8")

    with as_machine(home):
        with db.pool(gate=database_gate) as registry_pool:
            reg.register_tree(
                registry_pool, root=version_one, name="edit-demo", kind="skill", source="local:home"
            )
        assert mat.materialize_skills(
            db_conn,
            dest_root=skills_root,
            last_written=partial(mat.last_written_hash, home=home),
        ).landed == ["edit-demo"]
        (skills_root / "edit-demo" / "SKILL.md").write_text(
            _SKILL.format(body="MY LOCAL EDIT"), encoding="utf-8"
        )
        with db.pool(gate=database_gate) as registry_pool:
            reg.register_tree(
                registry_pool, root=version_two, name="edit-demo", kind="skill", source="local:home"
            )

        materialize_cluster_extensions(
            database_factory=operator_database, skills_root=skills_root, home=home
        )

    assert "MY LOCAL EDIT" in (skills_root / "edit-demo" / "SKILL.md").read_text(encoding="utf-8")
    assert emitted_events.attributes("converge_file_preserved") == [
        {"path": str(skills_root / "edit-demo"), "key": "edit-demo", "surface": "extensions"}
    ]
