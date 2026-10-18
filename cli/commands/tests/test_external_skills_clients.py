from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from base.cluster.machine import MachineIdentity
from base.config import ConfigBoot
from base.host.system.tests.fake_commands import fake_host
from base.telemetry import EventPipeline
from cli.commands.extensions import external_skills as bridge
from cli.database.tests.command_inputs import command_config_machine as command_config_machine
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.factories.external_skills import skill_ctx
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline

# meiva:note
# One (home directory, ledger key) pair per supported surface; the bridge publishes
# into `<home>/skills/<skill>` for each of them.
CLIENTS = (
    (".codex", "codex"),
    (".claude", "claude"),
    (".gemini", "gemini"),
    (".grok", "grok"),
    (".qwen", "qwen"),
    (".cursor", "cursor"),
    (".gemini/config", "antigravity"),
    (".gemini/antigravity-cli", "antigravity-cli"),
    (".dsh", "dsh"),
)
PUBLISHED_SKILLS = ("meiva-guide", "impersonator-guide")
MARKER_NAME = ".meiva-managed.json"


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    for name in PUBLISHED_SKILLS:
        source = repo / "meiva_builtins" / "skills" / "platform" / name
        (source / "references").mkdir(parents=True)
        (source / "SKILL.md").write_text(f"# {name}\n")
        (source / "references" / "details.md").write_text(f"{name} details\n")
    return repo


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


def _ctx(
    repo: Path,
    tmp_path: Path,
    *,
    operator_database: Callable[[], Any],
    producer: Callable[[], EventPipeline],
    command_config_machine: tuple[ConfigBoot, MachineIdentity],
):
    return skill_ctx(
        repo,
        tmp_path,
        operator_database=operator_database,
        producer=producer,
        command_config_machine=command_config_machine,
    )


def _run(
    repo: Path,
    home: Path,
    tmp_path: Path,
    *,
    operator_database: Callable[[], Any],
    producer: Callable[[], EventPipeline],
    command_config_machine: tuple[ConfigBoot, MachineIdentity],
) -> None:
    bridge.converge_external_agent_skill(
        _ctx(
            repo,
            tmp_path,
            operator_database=operator_database,
            producer=producer,
            command_config_machine=command_config_machine,
        ),
        host_home=home,
    )


def _ledger_names(tmp_path: Path) -> set[str]:
    root = tmp_path / "meiva-home" / "configs" / "external-agent-skills"
    return {path.name for path in root.glob("*.json")}


def _snapshot(root: Path) -> dict[str, tuple[str, bytes]]:
    snapshot: dict[str, tuple[str, bytes]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        snapshot[relative] = ("dir", b"") if path.is_dir() else ("file", path.read_bytes())
    return snapshot


def test_every_present_client_receives_both_skills(
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentity],
) -> None:
    repo = _repo(tmp_path)
    home = _home(tmp_path)
    for home_dir, _key in CLIENTS:
        (home / home_dir).mkdir(parents=True)
        (home / home_dir / "settings.json").write_text(f"{home_dir} settings\n")

    _run(
        repo,
        home,
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_config_machine=command_config_machine,
    )

    for home_dir, _key in CLIENTS:
        for skill in PUBLISHED_SKILLS:
            target = home / home_dir / "skills" / skill
            assert (target / "SKILL.md").read_bytes() == (
                repo / "meiva_builtins" / "skills" / "platform" / skill / "SKILL.md"
            ).read_bytes()
            assert (target / "references" / "details.md").is_file()
            marker = json.loads((target / MARKER_NAME).read_text())
            assert marker["owner"] == "meiva"
            assert marker["skill"] == skill
        assert (home / home_dir / "settings.json").read_text() == f"{home_dir} settings\n"

    assert _ledger_names(tmp_path) == {
        f"{key}-{skill}.json" for _home_dir, key in CLIENTS for skill in PUBLISHED_SKILLS
    }


def test_absent_homes_and_absent_programs_leave_no_directory(
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentity],
) -> None:
    repo = _repo(tmp_path)
    home = _home(tmp_path)

    _run(
        repo,
        home,
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_config_machine=command_config_machine,
    )

    assert list(home.iterdir()) == []
    assert _ledger_names(tmp_path) == set()


def test_installed_program_without_home_publishes_and_creates_only_that_home(
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentity],
) -> None:
    repo = _repo(tmp_path)
    home = _home(tmp_path)
    context = _ctx(
        repo,
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_config_machine=command_config_machine,
    )
    installed_elsewhere = replace(context, host=fake_host(programs={"gemini": "/usr/bin/gemini"}))

    bridge.converge_external_agent_skill(installed_elsewhere, host_home=home)

    assert [path.name for path in home.iterdir()] == [".gemini"]
    for skill in PUBLISHED_SKILLS:
        assert (home / ".gemini" / "skills" / skill / MARKER_NAME).is_file()
    assert _ledger_names(tmp_path) == {f"gemini-{skill}.json" for skill in PUBLISHED_SKILLS}


def test_second_converge_is_byte_and_timestamp_idempotent_for_new_clients(
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentity],
) -> None:
    repo = _repo(tmp_path)
    home = _home(tmp_path)
    (home / ".gemini").mkdir()
    (home / ".grok").mkdir()
    context = _ctx(
        repo,
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_config_machine=command_config_machine,
    )

    bridge.converge_external_agent_skill(context, host_home=home)
    targets = [
        home / name / "skills" / skill
        for name in (".gemini", ".grok")
        for skill in PUBLISHED_SKILLS
    ]
    before = {target: _snapshot(target) for target in targets}
    mtimes = {
        target: {path.relative_to(target): path.stat().st_mtime_ns for path in target.rglob("*")}
        for target in targets
    }

    bridge.converge_external_agent_skill(context, host_home=home)

    assert {target: _snapshot(target) for target in targets} == before
    assert {
        target: {path.relative_to(target): path.stat().st_mtime_ns for path in target.rglob("*")}
        for target in targets
    } == mtimes
    for name in (".gemini", ".grok"):
        assert list((home / name / "skills").glob(".*meiva-*")) == []


def test_unmanaged_target_in_one_client_is_preserved_and_its_other_skill_still_publishes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentity],
) -> None:
    repo = _repo(tmp_path)
    home = _home(tmp_path)
    (home / ".gemini").mkdir()
    (home / ".qwen").mkdir()
    unmanaged = home / ".gemini" / "skills" / "meiva-guide"
    unmanaged.mkdir(parents=True)
    (unmanaged / "SKILL.md").write_text("my own skill\n")

    _run(
        repo,
        home,
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_config_machine=command_config_machine,
    )

    assert (unmanaged / "SKILL.md").read_text() == "my own skill\n"
    assert not (unmanaged / MARKER_NAME).exists()
    assert not (unmanaged / "references").exists()
    assert (home / ".gemini" / "skills" / "impersonator-guide" / MARKER_NAME).is_file()
    for skill in PUBLISHED_SKILLS:
        assert (home / ".qwen" / "skills" / skill / MARKER_NAME).is_file()
    assert _ledger_names(tmp_path) == {
        "gemini-impersonator-guide.json",
        "qwen-meiva-guide.json",
        "qwen-impersonator-guide.json",
    }
    assert "Gemini CLI external operator skill skipped" in capsys.readouterr().err


def test_generation_shaped_residue_in_a_new_client_is_never_adopted(
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentity],
) -> None:
    repo = _repo(tmp_path)
    home = _home(tmp_path)
    residue = home / ".cursor" / "skills" / f".meiva-guide.meiva-stage-{'0' * 32}"
    residue.mkdir(parents=True)
    (residue / "SKILL.md").write_text("someone else's staged copy\n")

    _run(
        repo,
        home,
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_config_machine=command_config_machine,
    )

    assert (residue / "SKILL.md").read_text() == "someone else's staged copy\n"
    for skill in PUBLISHED_SKILLS:
        assert (home / ".cursor" / "skills" / skill / MARKER_NAME).is_file()
