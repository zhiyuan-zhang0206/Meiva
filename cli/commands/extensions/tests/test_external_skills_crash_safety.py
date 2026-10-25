from __future__ import annotations

import json
import os
import shutil
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from base.clock import SYSTEM_TIMERS
from base.cluster.machine import MachineIdentityReader
from base.config import ConfigBoot
from base.host.system.tests.fake_commands import fake_host
from base.sessions.tests.refuse_helper import refuse_helper
from base.telemetry import EventPipeline
from cli.commands.converge.spec import ConvergeCtx
from cli.commands.converge.tests.external_skills import (
    client_home,
    skill_ctx,
    skill_source,
    target_path,
)
from cli.commands.extensions import external_skills as bridge
from cli.commands.extensions.external_skill_host import filesystem as bridge_fs
from cli.commands.extensions.tests.support import killed_converge as killed
from cli.database import OperatorDatabaseFactory
from cli.database.runtime import UserSession
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.cli_tests import operator_pipeline as operator_pipeline
from cli.database.tests.command_inputs import command_config_machine as command_config_machine
from cli.database.tests.command_inputs import command_runtime as command_runtime

SKILL = "operating-meiva-cluster"


def _converge(ctx: ConvergeCtx, *, host_home: Path | None = None) -> None:
    bridge.converge_external_agent_skill(ctx, host_home=host_home, skill_names=(SKILL,))


def _world(
    tmp_path: Path,
    *,
    operator_database: OperatorDatabaseFactory,
    producer: Callable[[], EventPipeline],
    command_config_machine: tuple[ConfigBoot, MachineIdentityReader],
) -> tuple[Path, Path, ConvergeCtx]:
    config, machine = command_config_machine
    repo = tmp_path / "repo"
    source = repo / "meiva_builtins" / "skills" / "platform" / SKILL
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("operator v1\n")
    host_home = tmp_path / "host-home"
    client = host_home / ".codex"
    client.mkdir(parents=True)
    meiva_home = tmp_path / "meiva-home"
    (meiva_home / "configs").mkdir(parents=True)
    return (
        source,
        client,
        ConvergeCtx(
            repo=repo,
            meiva_home=meiva_home,
            roles=None,
            config=config,
            database_factory=operator_database,
            producer=producer,
            machine=machine,
            helper=refuse_helper,
            host=fake_host(),
            session=UserSession(user_home=meiva_home, login_shell="/bin/zsh", interactive=False),
            services=frozenset(),
            timers=SYSTEM_TIMERS,
        ),
    )


def _target(client: Path) -> Path:
    return client / "skills" / SKILL


def _ledger(context: ConvergeCtx) -> dict[str, Any]:
    value: object = json.loads(
        (context.meiva_home / "configs" / "external-agent-skills" / "codex.json").read_text()
    )
    return cast(dict[str, Any], value)


def _residues(client: Path) -> list[Path]:
    return list((client / "skills").glob(f".{SKILL}.meiva-*"))


def _remove_by_hand(client: Path, context: ConvergeCtx) -> None:
    for residue in _residues(client):
        shutil.rmtree(residue)
    ledger_root = context.meiva_home / "configs" / "external-agent-skills"
    for stage in ledger_root.glob(".codex-stage-*"):
        shutil.rmtree(stage)
    (ledger_root / "codex.json").unlink()


def _site(context: ConvergeCtx, client: Path, source: Path) -> killed.Site:
    return killed.Site(
        skills_root=client / "skills",
        ledger_path=context.meiva_home / "configs" / "external-agent-skills" / "codex.json",
        client_key="codex",
        skill=SKILL,
        source=source,
    )


# meiva:note
# A process killed after the staged copy was renamed into place, and before that was recorded,
# leaves the stage on disk and the ledger at `publishing`. The next converge reports the
# interrupted publication and touches nothing; once a person has removed the residue and the
# ledger, the skill installs.
def test_interrupted_stage_publication_is_reported_and_never_resumed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentityReader],
) -> None:
    source, client, context = _world(
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_config_machine=command_config_machine,
    )
    target = _target(client)
    site = _site(context, client, source)
    killed.forget_publication(site, killed.stage(site))
    assert _ledger(context)["transaction"]["stage_state"] == "publishing"
    ledger_before = _ledger(context)
    residues_before = sorted(_residues(client))
    assert len(residues_before) == 1

    _converge(context, host_home=client.parent)

    assert "not resumed" in capsys.readouterr().err
    assert not target.exists()
    assert _ledger(context) == ledger_before
    assert sorted(_residues(client)) == residues_before

    _remove_by_hand(client, context)
    _converge(context, host_home=client.parent)

    assert (target / "SKILL.md").read_text() == "operator v1\n"
    assert _ledger(context)["transaction"] is None
    assert _residues(client) == []


# meiva:note
# A process killed after it renamed the managed target aside, and before it recorded the claim,
# leaves the ledger at `claiming`, the copy at its transaction-owned `previous` path and no target.
# A target a user creates in that gap is never replaced or adopted: the next converge reports
# the interrupted claim and touches nothing.
def test_interrupted_target_claim_is_reported_and_leaves_a_late_target_alone(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentityReader],
) -> None:
    source, client, context = _world(
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_config_machine=command_config_machine,
    )
    _converge(context, host_home=client.parent)
    target = _target(client)
    source.joinpath("SKILL.md").write_text("operator v2\n")
    site = _site(context, client, source)
    ledger = killed.stage(site)
    killed.claim_target(site, ledger)

    assert _ledger(context)["transaction"]["claim_state"] == "claiming"
    assert not target.exists()
    target.mkdir()
    (target / "user.txt").write_text("late user target\n")
    ledger_before = _ledger(context)
    residues_before = sorted(_residues(client))
    capsys.readouterr()

    _converge(context, host_home=client.parent)

    assert "not resumed" in capsys.readouterr().err
    assert (target / "user.txt").read_text() == "late user target\n"
    assert _ledger(context) == ledger_before
    assert sorted(_residues(client)) == residues_before

    _remove_by_hand(client, context)
    (target / "user.txt").unlink()
    target.rmdir()
    _converge(context, host_home=client.parent)

    assert (target / "SKILL.md").read_text() == "operator v2\n"
    assert _ledger(context)["transaction"] is None
    assert _residues(client) == []


# meiva:note
# A converge that has gone live with v2 and queued v1 as garbage, before any cleanup has run:
# the ledger holds the queued item (location `source`), the superseded copy sits at its
# transaction-owned `previous` path and the target is v2. Built with the bridge's own steps.
def _v2_live_with_garbage_queued(
    context: ConvergeCtx, client: Path, source: Path
) -> tuple[killed.Site, dict[str, Any], dict[str, Any], Path]:
    source.joinpath("SKILL.md").write_text("operator v2\n")
    site = _site(context, client, source)
    ledger = killed.stage(site)
    previous = killed.claim_target(site, ledger)
    killed.record_claim(site, ledger)
    killed.land_stage(site, ledger)
    item = killed.commit(site, ledger, previous)
    return site, ledger, item, previous


# meiva:note
# Cleanup moves the superseded copy into the private ledger directory and only then looks at what
# it moved. A copy tampered with before that, here a file swapped for a link to a file outside,
# fails the look: the copy is moved back, the item returns to `source`, nothing outside is
# touched, and once the tampering is undone the next converge finishes the cleanup.
def test_cleanup_root_claim_restores_tree_modified_before_private_isolation(
    tmp_path: Path,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentityReader],
) -> None:
    source, client, context = _world(
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_config_machine=command_config_machine,
    )
    _converge(context, host_home=client.parent)
    previous = _v2_live_with_garbage_queued(context, client, source)[3]
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n")
    outside.chmod(0o444)
    outside_before = outside.stat()
    swapped = previous / "SKILL.md"
    swapped.unlink()
    swapped.symlink_to(outside)

    _converge(context, host_home=client.parent)

    assert (_target(client) / "SKILL.md").read_text() == "operator v2\n"
    assert swapped.is_symlink()
    assert outside.read_text() == "outside\n"
    assert stat.S_IMODE(outside.stat().st_mode) == stat.S_IMODE(outside_before.st_mode)
    assert _ledger(context)["garbage"][0]["location"] == "source"

    swapped.unlink()
    swapped.write_text("operator v1\n")
    _converge(context, host_home=client.parent)

    assert _residues(client) == []
    assert _ledger(context)["garbage"] == []
    assert len(_ledger(context)["retained"]) == 1


# meiva:note
# A process killed after cleanup renamed the superseded copy into the ledger directory, and before
# it recorded that, leaves the item at `claiming` with the copy already moved. The next converge
# settles the claim from what is on disk and finishes the cleanup.
def test_restart_reconciles_cleanup_claim_before_post_write(
    tmp_path: Path,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentityReader],
) -> None:
    source, client, context = _world(
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_config_machine=command_config_machine,
    )
    _converge(context, host_home=client.parent)
    site, ledger, item, _previous = _v2_live_with_garbage_queued(context, client, source)
    killed.claim_cleanup_root(site, ledger, item, record=False)
    assert _ledger(context)["garbage"][0]["location"] == "claiming"

    _converge(context, host_home=client.parent)

    assert (_target(client) / "SKILL.md").read_text() == "operator v2\n"
    assert _ledger(context)["garbage"] == []
    assert len(_ledger(context)["retained"]) == 1
    assert _residues(client) == []


def test_unsupported_cleanup_never_changes_directory_permissions(tmp_path: Path) -> None:
    residue = tmp_path / "residue"
    nested = residue / "nested"
    nested.mkdir(parents=True)
    nested.chmod(0o500)
    manifest = bridge_fs.tree_manifest(residue)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o755)
    outside_before = outside.stat()

    with pytest.raises(bridge_fs.ClientConflictError):
        bridge_fs.remove_manifest_subset(residue, manifest)

    assert nested.is_dir()
    assert stat.S_IMODE(nested.stat().st_mode) == 0o500
    assert stat.S_IMODE(residue.stat().st_mode) == 0o755
    assert stat.S_IMODE(outside.stat().st_mode) == stat.S_IMODE(outside_before.st_mode)


def _claim_of(item: dict[str, Any], path: str) -> dict[str, str]:
    return next(claim for claim in item["file_claims"] if claim["path"] == path)


# meiva:note
# A process killed after cleanup renamed one file to its claim name, and before it recorded that,
# leaves the claim at `claiming` with the file already moved. Whether the object at the claim
# name is the one Meiva moved cannot be proved from the path, so the next converge adopts
# nothing: the claim is retained as it is.
def test_restart_does_not_adopt_ambiguous_per_file_claim(
    tmp_path: Path,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentityReader],
) -> None:
    source, client, context = _world(
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_config_machine=command_config_machine,
    )
    _converge(context, host_home=client.parent)
    site, ledger, item, _previous = _v2_live_with_garbage_queued(context, client, source)
    quarantine = killed.claim_cleanup_root(site, ledger, item)
    killed.claim_cleanup_file(site, ledger, quarantine, _claim_of(item, "SKILL.md"), record=False)
    ledger = _ledger(context)
    assert ledger["garbage"][0]["location"] == "quarantine"
    assert "claiming" in {claim["state"] for claim in ledger["garbage"][0]["file_claims"]}

    _converge(context, host_home=client.parent)

    ledger = _ledger(context)
    assert ledger["garbage"] == []
    assert len(ledger["retained"]) == 1
    assert all(claim["state"] == "retained" for claim in ledger["retained"][0]["file_claims"])
    assert (_target(client) / "SKILL.md").read_text() == "operator v2\n"


# meiva:note
# A path a user created at a file's claim name inside the private root is never adopted or
# overwritten: that claim is retained, the user's file keeps its content and the original file is
# left where it was.
def test_preexisting_per_file_quarantine_is_preserved_without_adoption(
    tmp_path: Path,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentityReader],
) -> None:
    source, client, context = _world(
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_config_machine=command_config_machine,
    )
    _converge(context, host_home=client.parent)
    site, ledger, item, _previous = _v2_live_with_garbage_queued(context, client, source)
    quarantine = killed.claim_cleanup_root(site, ledger, item)
    collision = quarantine / _claim_of(item, "SKILL.md")["quarantine"]
    collision.write_text("late user path\n")

    _converge(context, host_home=client.parent)

    assert collision.read_text() == "late user path\n"
    retained = _ledger(context)["retained"]
    assert len(retained) == 1
    assert _claim_of(retained[0], "SKILL.md")["state"] == "retained"
    assert (collision.parent / "SKILL.md").read_text() == "operator v1\n"
    assert (_target(client) / "SKILL.md").read_text() == "operator v2\n"


# meiva:note
# Once the superseded copy is in the private ledger directory, a file that cannot be claimed is
# retained for good: no pathname retry, no deletion. The failure is a real one: a nested directory
# is read-only, so the file in it cannot be renamed; the file beside it is claimed and verified.
def test_per_file_claim_failure_is_terminal_after_private_root_claim(
    tmp_path: Path,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentityReader],
) -> None:
    source, client, context = _world(
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_config_machine=command_config_machine,
    )
    references = source / "references"
    references.mkdir()
    (references / "recovery.md").write_text("recover safely\n")
    references.chmod(0o555)
    _converge(context, host_home=client.parent)
    source.joinpath("SKILL.md").write_text("operator v2\n")

    _converge(context, host_home=client.parent)

    ledger = _ledger(context)
    assert ledger["garbage"] == []
    (retained,) = ledger["retained"]
    assert {claim["path"]: claim["state"] for claim in retained["file_claims"]} == {
        ".meiva-managed.json": "retained",
        "SKILL.md": "retained",
        "references/recovery.md": "retained",
    }
    (quarantine,) = (context.meiva_home / "configs" / "external-agent-skills").glob(
        ".codex-retained-previous-*"
    )
    assert (quarantine / "references" / "recovery.md").read_text() == "recover safely\n"
    ledger_before = _ledger(context)

    _converge(context, host_home=client.parent)

    assert _ledger(context) == ledger_before
    assert (_target(client) / "SKILL.md").read_text() == "operator v2\n"


# meiva:note
# Removing a verified residue is refused outright: the check proves a file matches its record
# now, and a pathname could name something else by the time it is unlinked. Nothing is deleted,
# a link planted at the residue's path is left alone, and the file it points at keeps its
# content and its mode.
def test_cleanup_never_unlinks_a_verified_residue_by_pathname(tmp_path: Path) -> None:
    residue = tmp_path / "residue"
    residue.mkdir()
    victim = residue / "SKILL.md"
    victim.write_text("operator\n")
    manifest = bridge_fs.tree_manifest(residue)
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n")
    outside.chmod(0o444)
    outside_before = outside.stat()

    with pytest.raises(bridge_fs.ClientConflictError):
        bridge_fs.remove_manifest_subset(residue, manifest)

    assert victim.read_text() == "operator\n"
    victim.unlink()
    victim.symlink_to(outside)
    with pytest.raises(bridge_fs.ClientConflictError):
        bridge_fs.remove_manifest_subset(residue, manifest)
    assert victim.is_symlink()
    assert outside.read_text() == "outside\n"
    assert stat.S_IMODE(outside.stat().st_mode) == stat.S_IMODE(outside_before.st_mode)


# meiva:note
# A file claimed into the private root is looked at once more before it is recorded, and a
# replacement found there, here a link to a file outside, ends the claim as retained: the link is
# left in place, the file outside keeps its content and mode, and later converges do not revisit it.
def test_converge_terminally_records_post_verification_replacement(
    tmp_path: Path,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentityReader],
) -> None:
    source, client, context = _world(
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_config_machine=command_config_machine,
    )
    _converge(context, host_home=client.parent)
    site, ledger, item, _previous = _v2_live_with_garbage_queued(context, client, source)
    quarantine = killed.claim_cleanup_root(site, ledger, item)
    late_replacement = killed.claim_cleanup_file(
        site, ledger, quarantine, _claim_of(item, "SKILL.md")
    )
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n")
    outside.chmod(0o444)
    outside_before = outside.stat()
    late_replacement.rename(late_replacement.with_name(f"{late_replacement.name}.claimed"))
    late_replacement.symlink_to(outside)

    _converge(context, host_home=client.parent)

    assert late_replacement.is_symlink()
    assert outside.read_text() == "outside\n"
    assert stat.S_IMODE(outside.stat().st_mode) == stat.S_IMODE(outside_before.st_mode)
    ledger = _ledger(context)
    assert ledger["garbage"] == []
    assert len(ledger["retained"]) == 1
    assert all(claim["state"] == "retained" for claim in ledger["retained"][0]["file_claims"])

    _converge(context, host_home=client.parent)

    assert late_replacement.is_symlink()
    assert (_target(client) / "SKILL.md").read_text() == "operator v2\n"


@pytest.mark.parametrize("linked_component", ["client-home", "skills-root"])
def test_linked_external_roots_are_rejected_without_following(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    linked_component: str,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentityReader],
) -> None:
    repo = tmp_path / "repo"
    skill_source(repo)
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    client = host_home / ".codex"
    if linked_component == "client-home":
        client.symlink_to(outside, target_is_directory=True)
    else:
        client.mkdir()
        (client / "skills").symlink_to(outside, target_is_directory=True)
    _converge(
        skill_ctx(
            repo,
            tmp_path,
            operator_database=operator_database,
            producer=operator_pipeline,
            command_config_machine=command_config_machine,
        ),
        host_home=host_home,
    )

    assert not (outside / SKILL).exists()
    assert "Codex" in capsys.readouterr().err


def test_linked_host_home_is_rejected_without_inspection(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentityReader],
) -> None:
    repo = tmp_path / "repo"
    skill_source(repo)
    outside_home = tmp_path / "outside-home"
    (outside_home / ".codex").mkdir(parents=True)
    linked_home = tmp_path / "linked-home"
    linked_home.symlink_to(outside_home, target_is_directory=True)

    _converge(
        skill_ctx(
            repo,
            tmp_path,
            operator_database=operator_database,
            producer=operator_pipeline,
            command_config_machine=command_config_machine,
        ),
        host_home=linked_home,
    )

    assert not (outside_home / ".codex" / "skills").exists()
    assert "host home" in capsys.readouterr().err


def test_source_tree_link_is_fatal_before_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentityReader],
) -> None:
    repo = tmp_path / "repo"
    source = skill_source(repo)
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n")
    (source / "references" / "linked.md").symlink_to(outside)
    client = client_home(tmp_path)
    with pytest.raises(RuntimeError, match="source"):
        _converge(
            skill_ctx(
                repo,
                tmp_path,
                operator_database=operator_database,
                producer=operator_pipeline,
                command_config_machine=command_config_machine,
            ),
            host_home=client.parent,
        )

    assert not target_path(client, tmp_path).exists()


def test_source_path_component_link_is_fatal_before_copy(
    tmp_path: Path,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentityReader],
) -> None:
    repo = tmp_path / "repo"
    actual_agents = tmp_path / "actual-agents"
    source = actual_agents / "skills" / SKILL
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("operator\n")
    repo.mkdir()
    (repo / "meiva_builtins").symlink_to(actual_agents, target_is_directory=True)
    client = client_home(tmp_path)

    with pytest.raises(RuntimeError, match="source"):
        _converge(
            skill_ctx(
                repo,
                tmp_path,
                operator_database=operator_database,
                producer=operator_pipeline,
                command_config_machine=command_config_machine,
            ),
            host_home=client.parent,
        )

    assert not target_path(client, tmp_path).exists()


# meiva:note
# Cleanup keeps what it cannot prove it may delete: the superseded copy is moved into the private
# ledger directory and stays there, content intact, instead of being unlinked by pathname.
def test_cleanup_retains_private_residue_without_path_unlink(
    tmp_path: Path,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentityReader],
) -> None:
    repo = tmp_path / "repo"
    source = skill_source(repo)
    client = client_home(tmp_path)
    context = skill_ctx(
        repo,
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_config_machine=command_config_machine,
    )
    _converge(context, host_home=client.parent)
    target = target_path(client, tmp_path)
    (source / "SKILL.md").write_text("operator v2\n")

    _converge(context, host_home=client.parent)

    ledger_root = context.meiva_home / "configs" / "external-agent-skills"
    ledger = json.loads((ledger_root / "codex.json").read_text())
    assert ledger["transaction"] is None
    assert ledger["garbage"] == []
    assert [item["kind"] for item in ledger["retained"]] == ["previous"]
    assert ledger["retained"][0]["location"] == "retained"
    (retained,) = ledger_root.glob(".codex-retained-previous-*")
    old_copies = [
        p for p in retained.rglob("*") if p.is_file() and p.read_text() == "operator v1\n"
    ]
    assert len(old_copies) == 1

    _converge(context, host_home=client.parent)
    assert (target / "SKILL.md").read_text() == "operator v2\n"
    assert list(target.parent.glob(f".{SKILL}.meiva-*")) == []


def test_cleanup_rejects_same_content_hard_link_without_changing_outside_inode(
    tmp_path: Path,
) -> None:
    residue = tmp_path / "residue"
    residue.mkdir()
    residue_file = residue / "SKILL.md"
    residue_file.write_text("operator\n")
    residue_file.chmod(0o444)
    manifest = bridge_fs.tree_manifest(residue)
    residue_file.unlink()
    outside = tmp_path / "outside.md"
    outside.write_text("operator\n")
    outside.chmod(0o444)
    os.link(outside, residue_file)
    outside_before = outside.stat()

    with pytest.raises(bridge_fs.ClientConflictError, match="link"):
        bridge_fs.remove_manifest_subset(residue, manifest)

    assert residue_file.exists()
    assert outside.exists()
    assert residue_file.stat().st_ino == outside_before.st_ino
    assert stat.S_IMODE(outside.stat().st_mode) == stat.S_IMODE(outside_before.st_mode)


# meiva:note
# A path stat and an open-handle stat of the same file agree on identity (device and inode)
# whatever else the platform reports differently, such as the change time. Two stats that
# disagree on the inode, or that report no identity at all, are different objects.
def test_path_and_open_handle_metadata_variants_are_not_a_source_change() -> None:
    def stat_of(*, inode: int, device: int, ctime: int) -> os.stat_result:
        return os.stat_result((0o100644, inode, device, 1, 0, 0, 9, 0, 0, ctime))

    by_path = stat_of(inode=7, device=3, ctime=100)

    assert bridge_fs.same_identity(by_path, stat_of(inode=7, device=3, ctime=101))
    assert not bridge_fs.same_identity(by_path, stat_of(inode=8, device=3, ctime=100))
    assert not bridge_fs.same_identity(by_path, stat_of(inode=7, device=4, ctime=100))
    assert not bridge_fs.same_identity(stat_of(inode=0, device=0, ctime=1), by_path)
    assert not bridge_fs.same_identity(
        stat_of(inode=0, device=0, ctime=1), stat_of(inode=0, device=0, ctime=1)
    )
