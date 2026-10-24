# meiva:note
# What the writers under the observability commands promise on disk: `meiva lgtm render` publishes a
# world-readable UTF-8 file without leaving temporaries, and a rendered file is published owner-only
# with its hash recorded only after the write succeeded.

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from base.agents.context.clients import DatabaseFactory
from cli.commands.converge.rendered_file import write_rendered_guarded
from cli.commands.observability import grafana_render
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.command_inputs import command_runtime as command_runtime


def test_lgtm_render_publishes_a_public_utf8_file_without_leaving_temporaries(
    tmp_path: Path, operator_database: DatabaseFactory
) -> None:
    target = tmp_path / "lgtm/native/config/dashboards/meiva-ops-main.json"
    target.parent.mkdir(parents=True)
    old_umask = os.umask(0o077)
    try:
        rc = grafana_render.cmd_grafana_render(
            home=tmp_path, force=True, repo_only=True, database_factory=operator_database
        )
    finally:
        os.umask(old_umask)

    assert rc == 0
    assert json.loads(target.read_bytes().decode("utf-8"))["uid"] == "meiva-ops-main"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert sorted(path.name for path in target.parent.iterdir()) == [target.name]


def test_rendered_file_is_published_owner_only_with_its_hash_recorded(tmp_path: Path) -> None:
    target = tmp_path / "config" / "collector.yml"
    hashes = tmp_path / "hashes.json"
    target.parent.mkdir()

    warning = write_rendered_guarded(target, "café", hashes, "collector.yml", surface="test")

    assert warning is None
    assert target.read_bytes() == "café".encode()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert sorted(path.name for path in target.parent.iterdir()) == [target.name]
    assert list(json.loads(hashes.read_text(encoding="utf-8"))) == ["collector.yml"]


# meiva:note
# The hash record says what converge last wrote, so it moves only after the file is in place: a
# destination that cannot be replaced (here a directory) fails the write and leaves the record as
# it was, rather than claiming a render that never landed.
def test_a_failed_write_does_not_advance_the_hash_record(tmp_path: Path) -> None:
    target = tmp_path / "config" / "collector.yml"
    target.mkdir(parents=True)
    hashes = tmp_path / "hashes.json"
    hashes.write_text(json.dumps({"other.yml": "recorded"}), encoding="utf-8")

    with pytest.raises(OSError, match="Is a directory"):
        write_rendered_guarded(target, "rendered", hashes, "collector.yml", surface="test")

    assert json.loads(hashes.read_text(encoding="utf-8")) == {"other.yml": "recorded"}
    assert sorted(path.name for path in target.parent.iterdir()) == [target.name]
    assert target.is_dir()
