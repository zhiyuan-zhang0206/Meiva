# meiva:note
# `meiva lgtm render` against a real home: the target is `lgtm/native/config/dashboards/` under
# `tmp_path`, the render is the real metric-registry render (checkout plugins only), and the
# database factory is the operator one the command is given.

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from base.agents.context.clients import DatabaseFactory
from cli.commands.observability import grafana_render
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.command_inputs import command_runtime as command_runtime

HASHES = Path("lgtm/native/config/provisioning-hashes.json")


@pytest.fixture
def target(tmp_path: Path) -> Path:
    target = tmp_path / "lgtm/native/config/dashboards/meiva-ops-main.json"
    target.parent.mkdir(parents=True)
    return target


def _render(home: Path, database: DatabaseFactory, *, force: bool) -> int:
    return grafana_render.cmd_grafana_render(
        home=home, force=force, repo_only=True, database_factory=database
    )


def test_render_cmd_previews_writes_and_refreshes(
    tmp_path: Path,
    target: Path,
    capsys: pytest.CaptureFixture[str],
    operator_database: DatabaseFactory,
) -> None:
    assert _render(tmp_path, operator_database, force=False) == 1
    assert not target.is_file()

    assert _render(tmp_path, operator_database, force=True) == 0
    written = target.read_text(encoding="utf-8")
    assert '"uid": "meiva-ops-main"' in written

    assert _render(tmp_path, operator_database, force=False) == 0

    drifted = written.replace("LLM calls (window)", "LLM calls (window) EDITED")
    assert drifted != written
    target.write_text(drifted, encoding="utf-8")
    assert _render(tmp_path, operator_database, force=False) == 1
    assert "render differs" in capsys.readouterr().err
    assert target.read_text(encoding="utf-8") == drifted

    assert _render(tmp_path, operator_database, force=True) == 0
    assert target.read_text(encoding="utf-8") == written


def test_render_cmd_refuses_a_host_without_the_lgtm_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], operator_database: DatabaseFactory
) -> None:
    assert _render(tmp_path, operator_database, force=True) == 1

    assert "does not run the LGTM observability stack" in capsys.readouterr().err
    assert not (tmp_path / "lgtm").exists()


def test_render_cmd_force_records_the_dashboard_hash(
    tmp_path: Path, target: Path, operator_database: DatabaseFactory
) -> None:
    assert _render(tmp_path, operator_database, force=True) == 0

    hashes = json.loads((tmp_path / HASHES).read_text(encoding="utf-8"))
    assert (
        hashes["dashboards/meiva-ops-main.json"] == hashlib.sha256(target.read_bytes()).hexdigest()
    )
    assert hashes["$dashboard-source"] == "render"


def test_render_cmd_records_the_hash_when_the_file_is_already_in_sync(
    tmp_path: Path, target: Path, operator_database: DatabaseFactory
) -> None:
    assert _render(tmp_path, operator_database, force=True) == 0
    (tmp_path / HASHES).unlink()

    assert _render(tmp_path, operator_database, force=False) == 0

    hashes = json.loads((tmp_path / HASHES).read_text(encoding="utf-8"))
    assert (
        hashes["dashboards/meiva-ops-main.json"] == hashlib.sha256(target.read_bytes()).hexdigest()
    )
    assert hashes["$dashboard-source"] == "render"
