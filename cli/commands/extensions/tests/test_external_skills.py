from __future__ import annotations

import copy
import json
import os
import random
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from base.cluster.machine import MachineIdentityReader
from base.config import ConfigBoot
from base.host.private_storage import write_private_bytes
from base.host.system.tests.fake_commands import fake_host
from base.sessions.tests.refuse_helper import refuse_helper
from base.telemetry import EventPipeline
from cli.commands.converge import spec as converge_host
from cli.commands.extensions import external_skills as external_skills_module
from cli.commands.extensions.external_skill_host import filesystem as filesystem_module
from cli.commands.extensions.external_skill_host.ledger import load_ledger, load_retained_items
from cli.commands.extensions.external_skill_host.records import CleanupItem
from cli.commands.extensions.tests.support import killed_converge as killed
from cli.commands.extensions.tests.support.source_bindings import PROD_ELSEWHERE
from cli.database import OperatorDatabaseFactory
from cli.database.runtime import UserSession
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.cli_tests import operator_pipeline as operator_pipeline
from cli.database.tests.command_inputs import command_config_machine as command_config_machine
from cli.database.tests.command_inputs import command_runtime as command_runtime
from cli.database.tests.command_inputs import inert_terminal
from meiva.sdk_surface.tests.context_home import context_over_unit_home as context_over_unit_home

SKILL_NAME = "operating-meiva-cluster"
MARKER_NAME = ".meiva-managed.json"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    host_home = tmp_path / "home"
    host_home.mkdir()
    assert host_home.resolve().is_relative_to(tmp_path.resolve())
    return host_home


def _bridge_module():
    return external_skills_module


def _filesystem_module():
    return filesystem_module


def _write_source(repo: Path, *, body: str = "operator v1\n") -> Path:
    source = repo / "meiva_builtins" / "skills" / "platform" / SKILL_NAME
    (source / "references").mkdir(parents=True)
    (source / "SKILL.md").write_text(body)
    (source / "references" / "recovery.md").write_text("recover safely\n")
    return source


def _ctx(
    repo: Path,
    home: Path,
    *,
    operator_database: OperatorDatabaseFactory,
    producer: Callable[[], EventPipeline],
    command_config_machine: tuple[ConfigBoot, MachineIdentityReader],
) -> converge_host.ConvergeCtx:
    config, machine = command_config_machine
    meiva_home = home / ".meiva"
    (meiva_home / "configs").mkdir(parents=True, exist_ok=True)
    return converge_host.ConvergeCtx(
        secure_rng=random.SystemRandom(),
        repo=repo,
        meiva_home=meiva_home,
        roles=None,
        config=config,
        database_factory=operator_database,
        producer=producer,
        machine=machine,
        helper=refuse_helper,
        host=fake_host(uid=os.geteuid()),
        session=UserSession(user_home=meiva_home, login_shell="/bin/zsh", interactive=False),
        services=frozenset(),
        timers=SYSTEM_TIMERS,
        clock=Clock(
            ClockConfig("UTC", None, message_timestamp_weekday=False), timers=SYSTEM_TIMERS
        ),
        terminal=inert_terminal(),
    )


def _target(client_home: Path) -> Path:
    target = client_home / "skills" / SKILL_NAME
    assert target.resolve(strict=False).is_relative_to(client_home.parent.resolve())
    return target


def _converge(
    ctx: converge_host.ConvergeCtx,
    *,
    host_home: Path | None = None,
    skill_names: tuple[str, ...] = (SKILL_NAME,),
) -> None:
    _bridge_module().converge_external_agent_skill(
        ctx,
        host_home=host_home,
        skill_names=skill_names,
    )


def _run(
    repo: Path,
    home: Path,
    *,
    operator_database: OperatorDatabaseFactory,
    producer: Callable[[], EventPipeline],
    command_config_machine: tuple[ConfigBoot, MachineIdentityReader],
    skill_names: tuple[str, ...] = (SKILL_NAME,),
) -> None:
    _converge(
        _ctx(
            repo,
            home,
            operator_database=operator_database,
            producer=producer,
            command_config_machine=command_config_machine,
        ),
        host_home=home,
        skill_names=skill_names,
    )


def _tree_snapshot(root: Path) -> dict[str, tuple[str, bytes]]:
    snapshot: dict[str, tuple[str, bytes]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            snapshot[relative] = ("dir", b"")
        else:
            snapshot[relative] = ("file", path.read_bytes())
    return snapshot


def _temp_entries(skills_root: Path) -> list[Path]:
    return list(skills_root.glob(f".{SKILL_NAME}.meiva-*"))


def test_absent_client_homes_are_not_created(
    home: Path,
    tmp_path: Path,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentityReader],
) -> None:
    repo = tmp_path / "repo"

    _run(
        repo,
        home,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_config_machine=command_config_machine,
    )

    assert not (home / ".codex").exists()
    assert not (home / ".claude").exists()


def test_present_codex_and_claude_homes_receive_complete_managed_copy(
    home: Path,
    tmp_path: Path,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentityReader],
) -> None:
    repo = tmp_path / "repo"
    source = _write_source(repo)
    for client in (".codex", ".claude"):
        client_home = home / client
        client_home.mkdir()
        (client_home / "settings.json").write_text(f"{client} settings\n")

    _run(
        repo,
        home,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_config_machine=command_config_machine,
    )

    for client in (".codex", ".claude"):
        client_home = home / client
        target = _target(client_home)
        assert (target / "SKILL.md").read_bytes() == (source / "SKILL.md").read_bytes()
        assert (target / "references" / "recovery.md").read_bytes() == (
            source / "references" / "recovery.md"
        ).read_bytes()
        marker = json.loads((target / MARKER_NAME).read_text())
        assert marker["owner"] == "meiva"
        assert marker["skill"] == SKILL_NAME
        assert marker["format"] == 6
        assert len(marker["source_digest"]) == 64
        assert len(marker["installation_id"]) == 32
        assert len(marker["generation_id"]) == 32
        assert (client_home / "settings.json").read_text() == f"{client} settings\n"


def test_second_converge_is_byte_and_timestamp_idempotent(
    home: Path,
    tmp_path: Path,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentityReader],
) -> None:
    repo = tmp_path / "repo"
    _write_source(repo)
    client_home = home / ".codex"
    client_home.mkdir()

    _converge(
        _ctx(
            repo,
            home,
            operator_database=operator_database,
            producer=operator_pipeline,
            command_config_machine=command_config_machine,
        ),
        host_home=home,
    )
    target = _target(client_home)
    before = _tree_snapshot(target)
    mtimes = {path.relative_to(target): path.stat().st_mtime_ns for path in target.rglob("*")}

    _converge(
        _ctx(
            repo,
            home,
            operator_database=operator_database,
            producer=operator_pipeline,
            command_config_machine=command_config_machine,
        ),
        host_home=home,
    )

    assert _tree_snapshot(target) == before
    assert {
        path.relative_to(target): path.stat().st_mtime_ns for path in target.rglob("*")
    } == mtimes
    assert _temp_entries(client_home / "skills") == []


def test_unmodified_managed_copy_updates_and_removes_only_stale_target_content(
    home: Path,
    tmp_path: Path,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentityReader],
) -> None:
    repo = tmp_path / "repo"
    source = _write_source(repo)
    client_home = home / ".claude"
    client_home.mkdir()
    _converge(
        _ctx(
            repo,
            home,
            operator_database=operator_database,
            producer=operator_pipeline,
            command_config_machine=command_config_machine,
        ),
        host_home=home,
    )
    unrelated = client_home / "skills" / "personal-skill" / "notes.txt"
    unrelated.parent.mkdir()
    unrelated.write_text("user owned\n")

    (source / "SKILL.md").write_text("operator v2\n")
    (source / "references" / "recovery.md").unlink()
    (source / "references" / "workspace.md").write_text("workspace lookup\n")
    _converge(
        _ctx(
            repo,
            home,
            operator_database=operator_database,
            producer=operator_pipeline,
            command_config_machine=command_config_machine,
        ),
        host_home=home,
    )

    target = _target(client_home)
    assert (target / "SKILL.md").read_text() == "operator v2\n"
    assert not (target / "references" / "recovery.md").exists()
    assert (target / "references" / "workspace.md").read_text() == "workspace lookup\n"
    assert unrelated.read_text() == "user owned\n"
    assert _temp_entries(client_home / "skills") == []


def test_unmanaged_preexisting_target_is_preserved_and_reported(
    home: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentityReader],
) -> None:
    repo = tmp_path / "repo"
    _write_source(repo)
    client_home = home / ".codex"
    target = _target(client_home)
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("user version\n")
    before = _tree_snapshot(target)

    _run(
        repo,
        home,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_config_machine=command_config_machine,
    )

    assert _tree_snapshot(target) == before
    assert "unmanaged" in capsys.readouterr().err
    assert _temp_entries(client_home / "skills") == []


def test_user_modified_managed_copy_is_preserved_and_reported(
    home: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentityReader],
) -> None:
    repo = tmp_path / "repo"
    source = _write_source(repo)
    client_home = home / ".claude"
    client_home.mkdir()
    _converge(
        _ctx(
            repo,
            home,
            operator_database=operator_database,
            producer=operator_pipeline,
            command_config_machine=command_config_machine,
        ),
        host_home=home,
    )
    target = _target(client_home)
    (target / "SKILL.md").write_text("user customization\n")
    (target / "private-note.md").write_text("preserve me\n")
    (source / "SKILL.md").write_text("operator v2\n")
    before = _tree_snapshot(target)

    _converge(
        _ctx(
            repo,
            home,
            operator_database=operator_database,
            producer=operator_pipeline,
            command_config_machine=command_config_machine,
        ),
        host_home=home,
    )

    assert _tree_snapshot(target) == before
    assert "user-modified" in capsys.readouterr().err
    assert _temp_entries(client_home / "skills") == []


def test_failed_update_restores_previous_copy_and_cleans_only_its_staging_dir(
    home: Path,
    tmp_path: Path,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentityReader],
) -> None:
    repo = tmp_path / "repo"
    source = _write_source(repo)
    client_home = home / ".codex"
    client_home.mkdir()
    module = _bridge_module()
    _converge(
        _ctx(
            repo,
            home,
            operator_database=operator_database,
            producer=operator_pipeline,
            command_config_machine=command_config_machine,
        ),
        host_home=home,
    )
    target = _target(client_home)
    before = _tree_snapshot(target)
    unrelated = client_home / "skills" / ".someone-elses-staging"
    unrelated.mkdir()
    (unrelated / "keep").write_text("untouched\n")
    (source / "SKILL.md").write_text("operator v2\n")
    original_rename = module.rename_no_replace
    failed = False

    def fail_staged_activation(path: Path, destination: Path) -> None:
        nonlocal failed
        if (
            not failed
            and path.name.startswith(f".{SKILL_NAME}.meiva-stage-")
            and destination == target
        ):
            failed = True
            raise OSError("injected activation failure")
        original_rename(path, destination)

    module.converge_external_agent_skill(
        _ctx(
            repo,
            home,
            operator_database=operator_database,
            producer=operator_pipeline,
            command_config_machine=command_config_machine,
        ),
        host_home=home,
        skill_names=(SKILL_NAME,),
        rename=fail_staged_activation,
    )

    assert _tree_snapshot(target) == before
    assert (unrelated / "keep").read_text() == "untouched\n"
    _converge(
        _ctx(
            repo,
            home,
            operator_database=operator_database,
            producer=operator_pipeline,
            command_config_machine=command_config_machine,
        ),
        host_home=home,
    )
    assert (target / "SKILL.md").read_text() == "operator v2\n"
    assert _temp_entries(client_home / "skills") == []


_DISTRIBUTED_SKILLS = ("operating-meiva-cluster", "deploy-meiva-cluster")


def _write_deploy_source(repo: Path) -> Path:
    source = repo / "meiva_builtins" / "skills" / "platform" / "deploy-meiva-cluster"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("deployment v1\n")
    legacy = repo / ".agents" / "skills" / SKILL_NAME
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text("operator v1\n")
    return source


@pytest.mark.parametrize("client", [".codex", ".claude"])
def test_both_operator_skills_install_and_update_independently(
    home: Path,
    tmp_path: Path,
    client: str,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentityReader],
) -> None:
    repo = tmp_path / "repo"
    operate = _write_source(repo)
    deploy = _write_deploy_source(repo)
    client_home = home / client
    client_home.mkdir()
    _run(
        repo,
        home,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_config_machine=command_config_machine,
        skill_names=_DISTRIBUTED_SKILLS,
    )
    targets = client_home / "skills"
    operate_before = _tree_snapshot(targets / SKILL_NAME)
    for name in _DISTRIBUTED_SKILLS:
        marker = json.loads((targets / name / MARKER_NAME).read_text())
        assert marker["skill"] == name
    (deploy / "SKILL.md").write_text("deployment v2\n")
    _run(
        repo,
        home,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_config_machine=command_config_machine,
        skill_names=_DISTRIBUTED_SKILLS,
    )
    assert (targets / "deploy-meiva-cluster" / "SKILL.md").read_text() == "deployment v2\n"
    assert _tree_snapshot(targets / SKILL_NAME) == operate_before
    (operate / "SKILL.md").write_text("operator v2\n")
    _run(
        repo,
        home,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_config_machine=command_config_machine,
        skill_names=_DISTRIBUTED_SKILLS,
    )
    assert (targets / SKILL_NAME / "SKILL.md").read_text() == "operator v2\n"
    assert (targets / "deploy-meiva-cluster" / "SKILL.md").read_text() == "deployment v2\n"
    ledger_root = home / ".meiva" / "configs" / "external-agent-skills"
    key = "codex" if client == ".codex" else "claude"
    assert (ledger_root / f"{key}.json").is_file()
    assert (ledger_root / f"{key}-deploy-meiva-cluster.json").is_file()


def test_operator_conflict_does_not_block_deployment_update(
    home: Path,
    tmp_path: Path,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentityReader],
) -> None:
    repo = tmp_path / "repo"
    _write_source(repo)
    deploy = _write_deploy_source(repo)
    (home / ".codex").mkdir()
    _run(
        repo,
        home,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_config_machine=command_config_machine,
        skill_names=_DISTRIBUTED_SKILLS,
    )
    target = _target(home / ".codex")
    (target / "SKILL.md").write_text("user customization\n")
    (deploy / "SKILL.md").write_text("deployment v2\n")
    _run(
        repo,
        home,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_config_machine=command_config_machine,
        skill_names=_DISTRIBUTED_SKILLS,
    )
    assert (target / "SKILL.md").read_text() == "user customization\n"
    assert (target.parent / "deploy-meiva-cluster" / "SKILL.md").read_text() == "deployment v2\n"


# meiva:note
# A process killed while it published the deployment skill's staged copy leaves that skill's
# ledger at `publishing`. The next converge reports it as not resumed, leaves the deployment
# skill uninstalled and the operator skill, which has its own ledger, exactly as it was.
def test_deployment_publication_crash_is_reported_without_changing_operator(
    home: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentityReader],
) -> None:
    repo = tmp_path / "repo"
    _write_source(repo)
    deploy = _write_deploy_source(repo)
    (home / ".codex").mkdir()
    _run(
        repo,
        home,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_config_machine=command_config_machine,
    )
    operator_before = _tree_snapshot(_target(home / ".codex"))
    site = killed.Site(
        skills_root=home / ".codex" / "skills",
        ledger_path=home
        / ".meiva"
        / "configs"
        / "external-agent-skills"
        / "codex-deploy-meiva-cluster.json",
        client_key="codex",
        skill="deploy-meiva-cluster",
        source=deploy,
    )
    killed.forget_publication(site, killed.stage(site))
    capsys.readouterr()

    _run(
        repo,
        home,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_config_machine=command_config_machine,
        skill_names=_DISTRIBUTED_SKILLS,
    )

    assert "not resumed" in capsys.readouterr().err
    assert _tree_snapshot(_target(home / ".codex")) == operator_before
    assert not (home / ".codex" / "skills" / "deploy-meiva-cluster").exists()


def test_product_guide_has_one_builtin_source_and_nested_operator_entries(unit_home: Path) -> None:
    from cli.commands.extensions.skills_sync import iter_sources

    repo = Path(__file__).resolve().parents[4]
    sources, _ = iter_sources(repo, home=unit_home)
    canonical = repo / "meiva_builtins" / "skills" / "platform" / "meiva-guide"
    assert (repo / ".agents" / "skills" / "meiva-guide").resolve() == canonical
    assert (repo / ".claude" / "skills" / "meiva-guide").resolve() == canonical
    assert (repo / ".meiva" / "skills" / "meiva-guide").resolve() == canonical
    assert any(source.name == "meiva-guide" and source.src == canonical for source in sources)
    assert not any(source.name in _DISTRIBUTED_SKILLS for source in sources)
    guide = (canonical / "SKILL.md").read_text()
    assert "deploy/SKILL.md" in guide
    assert "operations/SKILL.md" in guide


def test_builtin_guide_installs_with_the_core_update_channel(
    process_config: ConfigBoot, tmp_path: Path, unit_home: Path
) -> None:
    import shutil

    import meiva.skills
    from base.packages.extensions import install_registry
    from cli.commands.extensions.skills_sync import converge_skills

    product_repo = Path(__file__).resolve().parents[4]
    repo = tmp_path / "repo"
    sources = repo / "meiva_builtins" / "skills" / "platform"
    for name in ("meiva-guide",):
        shutil.copytree(
            product_repo / "meiva_builtins" / "skills" / "platform" / name, sources / name
        )
    converge_skills(repo, unit_home, binding=PROD_ELSEWHERE, now=datetime.now(UTC))
    installed_names = {skill["name"] for skill in meiva.skills.names()}
    for name in ("meiva-guide",):
        assert name in installed_names
        entry = install_registry.get(name, home=unit_home)
        assert entry is not None and entry.enabled and entry.origin == "repo"
        installed = unit_home / "skills" / name / "SKILL.md"
        original = installed.read_bytes()
        source = sources / name / "SKILL.md"
        source.write_bytes(original + b"\nUpdated product guidance.\n")
        converge_skills(repo, unit_home, binding=PROD_ELSEWHERE, now=datetime.now(UTC))
        assert installed.read_bytes() == original
        policy = install_registry.resolved_policy(
            entry, defaults_reader=lambda: process_config.view.packages
        )
        assert policy.channel == "core" and policy.mode == "auto"
        assert entry.origin_path == str(sources / name)


# meiva:note
# Without an explicit host home, the client homes are found under the invoking user's home as
# the converge context carries it: the session's, never one the bridge looks up itself.
def test_client_homes_are_found_under_the_sessions_user_home(
    home: Path,
    tmp_path: Path,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentityReader],
) -> None:
    repo = tmp_path / "repo"
    _write_source(repo)
    (home / ".codex").mkdir()
    ctx = _ctx(
        repo,
        home,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_config_machine=command_config_machine,
    )
    session = UserSession(user_home=home, login_shell="/bin/zsh", interactive=False)

    _converge(replace(ctx, session=session))

    assert _target(home / ".codex").is_dir()


def _seed_legacy_inline_retained(ledger_path: Path, count: int) -> None:
    (seed_item,) = load_retained_items(ledger_path)
    legacy_ledger = load_ledger(ledger_path, "codex")
    assert legacy_ledger is not None
    legacy_retained: list[CleanupItem] = []
    for index in range(1, count):
        retired = copy.deepcopy(seed_item)
        retired["path_generation_id"] = f"{index:032x}"
        legacy_retained.append(retired)
    legacy_ledger["retained"] = legacy_retained
    write_private_bytes(
        ledger_path, (json.dumps(legacy_ledger, indent=2, sort_keys=True) + "\n").encode()
    )


# meiva:note
# Live format-6 ledgers that accumulated inline `retained` entries before sidecar compaction
# load without conflict, archive every retired entry to its per-generation sidecar, and update
# a 120-file skill in a small bounded number of ledger writes instead of per-file fsyncs.
def test_large_skill_with_twenty_retained_generations_compacts_and_batches_ledger_writes(
    home: Path,
    tmp_path: Path,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentityReader],
) -> None:
    repo = tmp_path / "repo"
    source = _write_source(repo)
    for index in range(118):
        (source / "references" / f"doc_{index:03d}.md").write_text(f"reference {index}\n")
    (home / ".codex").mkdir()
    ctx = _ctx(
        repo,
        home,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_config_machine=command_config_machine,
    )
    _converge(ctx, host_home=home)
    (source / "SKILL.md").write_text("operator v2\n")
    _converge(ctx, host_home=home)

    ledger_path = ctx.meiva_home / "configs" / "external-agent-skills" / "codex.json"
    _seed_legacy_inline_retained(ledger_path, 20)
    assert ledger_path.stat().st_size > 700_000

    _converge(ctx, host_home=home)
    compacted_ledger = load_ledger(ledger_path, "codex")
    assert compacted_ledger is not None
    assert compacted_ledger["retained"] == []
    assert len(load_retained_items(ledger_path)) == 20
    assert ledger_path.stat().st_size < 40_000

    (source / "SKILL.md").write_text("operator v3\n")
    observed_payloads: set[bytes] = set()
    per_file_inodes: set[int] = set()
    per_file_renames = 0

    def record_materialize(snapshot: filesystem_module.SourceSnapshot, destination: Path) -> None:
        observed_payloads.add(ledger_path.read_bytes())
        external_skills_module.materialize_source_snapshot(snapshot, destination)

    def record_rename(src: Path, dst: Path) -> None:
        nonlocal per_file_renames
        observed_payloads.add(ledger_path.read_bytes())
        if ".meiva-retained-" in dst.name and src.parent == dst.parent:
            per_file_renames += 1
            per_file_inodes.add(ledger_path.lstat().st_ino)
        external_skills_module.rename_no_replace(src, dst)

    external_skills_module.converge_external_agent_skill(
        ctx,
        host_home=home,
        skill_names=(SKILL_NAME,),
        materialize=record_materialize,
        rename=record_rename,
    )
    observed_payloads.add(ledger_path.read_bytes())

    assert per_file_renames == 121
    assert len(per_file_inodes) == 1
    assert len(observed_payloads) <= 8
    assert ledger_path.stat().st_size < 40_000
    assert len(load_retained_items(ledger_path)) == 21
    assert (_target(home / ".codex") / "SKILL.md").read_text() == "operator v3\n"
