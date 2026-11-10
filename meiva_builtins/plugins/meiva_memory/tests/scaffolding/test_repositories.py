# meiva:note
# Provisioning's real repository effects and per-invocation target capture.
# meiva:link meiva_builtins/plugins/meiva_memory/docs/memory-pool-scaffold.md

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from base.cluster.machine import MachineNameMissing
from base.cluster.machine.boot import gateway_access_from_boot
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import ConfigBoot, get_field, set_field
from base.config.tests.configuration import snapshot_process_config
from base.deploy.git.memory_repo import (
    MemoryBranchMismatch,
    MemoryRemoteMissing,
    init_local_repo,
    run_git,
)
from base.deploy.git.tests.memory_checkouts import commit_note, git_http_remote, make_bundle
from base.deploy.git.tests.pool_server import PoolResponse, pool_server
from base.packages.plugins.loading.scaffold import ScaffoldFacts
from base.paths import gateway_vault_dir, meiva_home, vault_dir
from base.paths.tests.homes import unit_home as unit_home
from meiva_builtins.plugins.meiva_memory.setup import scaffold


@pytest.fixture(autouse=True)
def memory_configuration(process_config: ConfigBoot, unit_home: Path) -> Iterator[None]:
    fields = ("memory_remote", "memory_keep_local", "gateway_url", "cluster_secret")
    previous = {name: get_field(name) for name in fields}
    set_field("memory_remote", "")
    set_field("memory_keep_local", False)
    try:
        yield
    finally:
        for name, value in previous.items():
            set_field(name, value)


# meiva:note
# The facts `meiva memory init` reads once from its configuration owner, built from the process
# configuration as the test has set it by now (and the home this test points at, unless the test
# names one).
def _facts(home: Path | None = None) -> ScaffoldFacts:
    config = snapshot_process_config()
    general = config.view.general
    return ScaffoldFacts(
        home=meiva_home() if home is None else home,
        memory_keep_local=general.memory_keep_local,
        memory_remote=general.memory_remote,
        gateway=gateway_access_from_boot(config),
    )


@pytest.mark.parametrize("roles", ["agent-runner", "gateway,agent-runner"])
def test_existing_runner_mismatch_preserves_authored_work(
    machine_identity: ConfiguredMachineIdentity, roles: str
) -> None:
    machine_identity.set(role=roles, name="runner")
    target = vault_dir(home=meiva_home())
    init_local_repo("main", target)
    (target / "draft.md").write_text("unpublished")
    with pytest.raises(MemoryBranchMismatch, match="expected 'machine-runner'"):
        scaffold(machine=machine_identity, facts=_facts())
    assert run_git("branch", "--show-current", cwd=target) == "main"
    assert (target / "draft.md").read_text() == "unpublished"
    assert not (target / "memory" / "MEMORY.md").exists()


def test_existing_authoring_checkout_does_not_bootstrap_or_need_a_remote(
    machine_identity: ConfiguredMachineIdentity,
) -> None:
    machine_identity.set(role="agent-runner", name="runner")
    set_field("gateway_url", "")
    target = vault_dir(home=meiva_home())
    init_local_repo("machine-runner", target)
    curated = target / "memory" / "MEMORY.md"
    curated.parent.mkdir(exist_ok=True)
    curated.write_text("curated")
    before = run_git("rev-parse", "HEAD", cwd=target)
    scaffold(machine=machine_identity, facts=_facts())
    assert run_git("rev-parse", "HEAD", cwd=target) == before
    assert curated.read_text() == "curated"
    assert run_git("config", "--get", "core.hooksPath", cwd=target) == ".githooks"
    scaffold(machine=machine_identity, facts=_facts())
    assert curated.read_text() == "curated"


@pytest.mark.parametrize("roles", ["gateway", "gateway,agent-runner"])
def test_gateway_capable_without_remote_initializes_the_actual_checkouts(
    machine_identity: ConfiguredMachineIdentity, roles: str
) -> None:
    machine_identity.set(role=roles, name="runner")
    set_field("gateway_url", "")
    scaffold(machine=machine_identity, facts=_facts())
    authoring = vault_dir(home=meiva_home())
    consolidated = gateway_vault_dir(roles=machine_identity.role(), home=meiva_home())
    assert run_git("branch", "--show-current", cwd=authoring) == (
        "machine-runner" if "agent-runner" in machine_identity.role() else "main"
    )
    assert run_git("branch", "--show-current", cwd=consolidated) == "main"
    assert (authoring / ".gitignore").is_file()
    assert (consolidated / ".gitignore").is_file()
    assert run_git("remote", cwd=authoring) == ""
    assert run_git("remote", cwd=consolidated) == ""
    assert (authoring / "memory" / "MEMORY.md").is_file()


def test_gateway_only_does_not_require_a_machine_name() -> None:
    config = snapshot_process_config()
    config.set_field("machine_name", "")
    machine_identity = ConfiguredMachineIdentity(config)
    machine_identity.set(role="gateway")
    with pytest.raises(MachineNameMissing):
        machine_identity.name()
    scaffold(machine=machine_identity, facts=_facts())
    assert run_git("branch", "--show-current", cwd=vault_dir(home=meiva_home())) == "main"


def test_keep_local_runner_creates_without_gateway_access(
    machine_identity: ConfiguredMachineIdentity,
) -> None:
    machine_identity.set(role="agent-runner", name="runner")
    set_field("memory_keep_local", True)
    set_field("gateway_url", "")
    scaffold(machine=machine_identity, facts=_facts())
    root = vault_dir(home=meiva_home())
    assert run_git("branch", "--show-current", cwd=root) == "machine-runner"
    assert run_git("remote", cwd=root) == ""
    assert run_git("log", "-1", "--format=%s", cwd=root) == "init: local memory on machine-runner"


def test_keep_local_strips_all_remotes_and_switches_gateway_to_main(
    machine_identity: ConfiguredMachineIdentity,
) -> None:
    machine_identity.set(role="gateway", name="cloud")
    target = vault_dir(home=meiva_home())
    init_local_repo("main", target)
    run_git("checkout", "-b", "machine-cloud", cwd=target)
    run_git("remote", "add", "origin", "/unused-origin", cwd=target)
    run_git("remote", "add", "backup", "/unused-backup", cwd=target)
    set_field("memory_keep_local", True)
    scaffold(machine=machine_identity, facts=_facts())
    assert run_git("branch", "--show-current", cwd=target) == "main"
    assert run_git("remote", cwd=target) == ""


def test_split_runner_without_remote_bootstraps_a_real_gateway_bundle(
    tmp_path: Path,
    machine_identity: ConfiguredMachineIdentity,
) -> None:
    machine_identity.set(role="agent-runner", name="runner")
    head, bundle = make_bundle(tmp_path / "source")
    response = PoolResponse(bundle, head=head)
    set_field("cluster_secret", "pool-secret")
    with pool_server(response) as url:
        set_field("gateway_url", url)
        scaffold(machine=machine_identity, facts=_facts())
    root = vault_dir(home=meiva_home())
    assert run_git("rev-parse", "HEAD", cwd=root) == head
    assert run_git("branch", "--show-current", cwd=root) == "machine-runner"
    assert run_git("remote", cwd=root) == ""
    assert response.headers["Authorization"] == "Bearer pool-secret"
    assert (root / "memory" / "health" / "a.md").read_text() == "note"
    assert (root / ".githooks" / "pre-commit").is_file()


def test_split_runner_bootstrap_failure_is_not_an_empty_pool(
    machine_identity: ConfiguredMachineIdentity,
) -> None:
    machine_identity.set(role="agent-runner", name="runner")
    with pool_server(PoolResponse(b"unavailable", status=502)) as url:
        set_field("gateway_url", url)
        with pytest.raises(MemoryRemoteMissing, match=r"gateway pool snapshot.*HTTP 502"):
            scaffold(machine=machine_identity, facts=_facts())
    assert not (vault_dir(home=meiva_home()) / ".git").exists()
    assert not (vault_dir(home=meiva_home()) / "MEMORY.md").exists()


# meiva:note
# The checkouts sit under the home the facts name, not under the home the process environment
# names: the two differ here, so a scaffold that resolved the home itself would create the pool in
# the environment's. A combined unit has a second, consolidated checkout whose path also takes the
# home, so it is asserted too.
@pytest.mark.parametrize(
    ("roles", "has_consolidated"), [("agent-runner", False), ("gateway,agent-runner", True)]
)
def test_the_checkouts_sit_under_the_home_the_facts_name(
    tmp_path: Path,
    machine_identity: ConfiguredMachineIdentity,
    roles: str,
    has_consolidated: bool,
) -> None:
    machine_identity.set(role=roles, name="runner")
    explicit = tmp_path / "explicit-home"
    explicit.mkdir()
    assert explicit != meiva_home()
    set_field("memory_keep_local", True)

    scaffold(machine=machine_identity, facts=_facts(explicit))

    role_set = machine_identity.role()
    assert (vault_dir(home=explicit) / ".githooks" / "pre-commit").is_file()
    assert (gateway_vault_dir(roles=role_set, home=explicit) / ".git").is_dir() == has_consolidated
    assert not vault_dir(home=meiva_home()).exists()
    assert not gateway_vault_dir(roles=role_set, home=meiva_home()).exists()


def _two_remotes(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "initial-source"
    init_local_repo("main", source)
    head = commit_note(source, "initial remote")
    run_git("branch", "machine-runner", cwd=source)
    remote = tmp_path / "initial.git"
    run_git("clone", "--bare", str(source), str(remote), cwd=tmp_path)
    return remote, head


# meiva:note
# Both stages of a combined unit provision from the one set of facts the command read: the
# authoring checkout and the consolidated one clone the same remote, and a setting changed while
# the first stage is running (the live configuration here) does not reach the second.
def test_both_stages_of_a_combined_unit_use_the_facts_the_command_read(
    tmp_path: Path, machine_identity: ConfiguredMachineIdentity
) -> None:
    machine_identity.set(role="gateway,agent-runner", name="runner")
    authoring = vault_dir(home=meiva_home())
    consolidated = gateway_vault_dir(roles=machine_identity.role(), home=meiva_home())
    remote, first_head = _two_remotes(tmp_path)
    received, release = Event(), Event()
    with git_http_remote(remote, received=received, release=release) as url:
        set_field("memory_remote", url)
        facts = _facts()
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(scaffold, machine=machine_identity, facts=facts)
            try:
                assert received.wait(5), "authoring clone did not reach Git HTTP discovery"
                set_field("memory_keep_local", True)
                set_field("memory_remote", "")
            finally:
                release.set()
            pending.result(timeout=15)

    assert run_git("rev-parse", "HEAD", cwd=authoring) == first_head
    assert run_git("branch", "--show-current", cwd=authoring) == "machine-runner"
    assert (authoring / ".githooks" / "pre-commit").is_file()
    assert run_git("branch", "--show-current", cwd=consolidated) == "main"
    assert run_git("remote", "get-url", "origin", cwd=consolidated) == url
    assert run_git("rev-parse", "HEAD", cwd=consolidated) == first_head


def test_keep_local_facts_make_both_stages_of_a_combined_unit_local(
    machine_identity: ConfiguredMachineIdentity,
) -> None:
    machine_identity.set(role="gateway,agent-runner", name="runner")
    set_field("memory_keep_local", True)
    set_field("memory_remote", "http://unused.invalid/pool.git")

    scaffold(machine=machine_identity, facts=_facts())

    consolidated = gateway_vault_dir(roles=machine_identity.role(), home=meiva_home())
    assert run_git("remote", cwd=vault_dir(home=meiva_home())) == ""
    assert run_git("remote", cwd=consolidated) == ""
    assert run_git("log", "-1", "--format=%s", cwd=consolidated) == "init: local memory on main"


def test_existing_broken_checkout_preserves_the_git_exception(
    machine_identity: ConfiguredMachineIdentity,
) -> None:
    machine_identity.set(role="agent-runner", name="runner")
    (vault_dir(home=meiva_home()) / ".git").mkdir(parents=True)
    with pytest.raises(subprocess.CalledProcessError):
        scaffold(machine=machine_identity, facts=_facts())


def test_fresh_clone_git_failure_is_classified(
    machine_identity: ConfiguredMachineIdentity, tmp_path: Path
) -> None:
    machine_identity.set(role="agent-runner", name="runner")
    set_field("memory_remote", str(tmp_path / "not-a-repository"))
    with pytest.raises(RuntimeError, match="unrecognized failure shape") as failure:
        scaffold(machine=machine_identity, facts=_facts())
    assert isinstance(failure.value.__cause__, subprocess.CalledProcessError)


def test_legacy_checkout_requires_explicit_migration(
    machine_identity: ConfiguredMachineIdentity,
) -> None:
    machine_identity.set(role="gateway", name="runner")
    legacy = meiva_home() / "memory"
    init_local_repo("main", legacy)
    (legacy / "unpublished.md").write_text("keep this draft")
    before = run_git("rev-parse", "HEAD", cwd=legacy)
    with pytest.raises(RuntimeError, match="Legacy memory checkout found"):
        scaffold(machine=machine_identity, facts=_facts())
    assert (legacy / "unpublished.md").read_text() == "keep this draft"
    assert run_git("rev-parse", "HEAD", cwd=legacy) == before
    assert not vault_dir(home=meiva_home()).exists()


def test_existing_vault_with_old_root_notes_requires_migration(
    machine_identity: ConfiguredMachineIdentity,
) -> None:
    machine_identity.set(role="gateway", name="runner")
    root = vault_dir(home=meiva_home())
    init_local_repo("main", root)
    (root / "MEMORY.md").write_text("curated old layout")
    with pytest.raises(RuntimeError, match="legacy root-level notes"):
        scaffold(machine=machine_identity, facts=_facts())
    assert not (root / "memory").exists()
    assert (root / "MEMORY.md").read_text() == "curated old layout"


@pytest.mark.parametrize(("enabled", "url"), [(False, "https://lfs.invalid"), (True, " ")])
def test_disabled_or_unconfigured_lfs_creates_no_files(
    tmp_path: Path, enabled: bool, url: str
) -> None:
    from meiva_builtins.plugins.meiva_memory.default_config import MemoryConfig
    from meiva_builtins.plugins.meiva_memory.setup import configure_lfs

    configure_lfs(tmp_path, config=MemoryConfig(lfs_enabled=enabled, lfs_url=url))
    assert not (tmp_path / ".lfsconfig").exists()
    assert not (tmp_path / ".gitattributes").exists()


def test_lfs_convergence_preserves_custom_rules_and_updates_endpoint(tmp_path: Path) -> None:
    from meiva_builtins.plugins.meiva_memory.default_config import MemoryConfig
    from meiva_builtins.plugins.meiva_memory.setup import configure_lfs

    (tmp_path / ".gitattributes").write_text("*.txt text\n")
    config = MemoryConfig(lfs_enabled=True, lfs_url=" https://lfs.invalid/path ")
    configure_lfs(tmp_path, config=config)
    configure_lfs(tmp_path, config=config)
    assert (
        run_git("config", "--file", str(tmp_path / ".lfsconfig"), "--get", "lfs.url", cwd=tmp_path)
        == "https://lfs.invalid/path"
    )
    rules = (tmp_path / ".gitattributes").read_text().splitlines()
    assert rules[0] == "*.txt text"
    assert len(rules) == 7
    assert "*.parquet filter=lfs diff=lfs merge=lfs -text" in rules
