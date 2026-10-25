# meiva:note
# Provisioning's real repository effects and per-invocation target capture.
# meiva:link meiva_builtins/plugins/meiva_memory/docs/memory-pool-scaffold.meiva.okf.md

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from base.cluster.machine import MachineIdentity, MachineNameMissing
from base.cluster.machine.boot import machine_from_boot
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import get_field, set_field
from base.config.tests.configuration import snapshot_process_config
from base.deploy.git.memory_repo import (
    MemoryBranchMismatch,
    MemoryRemoteMissing,
    init_local_repo,
    run_git,
)
from base.deploy.git.tests.memory_checkouts import commit_note, git_http_remote, make_bundle
from base.deploy.git.tests.pool_server import PoolResponse, pool_server
from base.paths import gateway_memory_dir, memory_dir
from base.paths.tests.homes import point_home
from base.paths.tests.homes import unit_home as unit_home
from meiva_builtins.plugins.meiva_memory.setup import scaffold


@pytest.fixture(autouse=True)
def memory_configuration(process_config, unit_home: Path) -> Iterator[None]:
    fields = ("memory_remote", "memory_keep_local", "gateway_url", "cluster_secret")
    previous = {name: get_field(name) for name in fields}
    set_field("memory_remote", "")
    set_field("memory_keep_local", False)
    try:
        yield
    finally:
        for name, value in previous.items():
            set_field(name, value)


@pytest.mark.parametrize("roles", ["agent-runner", "gateway,agent-runner"])
def test_existing_runner_mismatch_preserves_authored_work(
    machine_identity: MachineIdentity, roles: str
) -> None:
    machine_identity.set(role=roles, name="runner")
    target = memory_dir()
    init_local_repo("main", target)
    (target / "draft.md").write_text("unpublished")
    with pytest.raises(MemoryBranchMismatch, match="expected 'machine-runner'"):
        scaffold(machine=machine_identity)
    assert run_git("branch", "--show-current", cwd=target) == "main"
    assert (target / "draft.md").read_text() == "unpublished"
    assert not (target / "MEMORY.md").exists()


def test_existing_authoring_checkout_does_not_bootstrap_or_need_a_remote(
    machine_identity: MachineIdentity,
) -> None:
    machine_identity.set(role="agent-runner", name="runner")
    set_field("gateway_url", "")
    target = memory_dir()
    init_local_repo("machine-runner", target)
    curated = target / "MEMORY.md"
    curated.write_text("curated")
    before = run_git("rev-parse", "HEAD", cwd=target)
    scaffold(machine=machine_identity)
    assert run_git("rev-parse", "HEAD", cwd=target) == before
    assert curated.read_text() == "curated"
    assert run_git("config", "--get", "core.hooksPath", cwd=target) == ".githooks"
    scaffold(machine=machine_identity)
    assert curated.read_text() == "curated"


@pytest.mark.parametrize("roles", ["gateway", "gateway,agent-runner"])
def test_gateway_capable_without_remote_initializes_the_actual_checkouts(
    machine_identity: MachineIdentity, roles: str
) -> None:
    machine_identity.set(role=roles, name="runner")
    set_field("gateway_url", "")
    scaffold(machine=machine_identity)
    authoring = memory_dir()
    consolidated = gateway_memory_dir(roles=machine_identity.role())
    assert run_git("branch", "--show-current", cwd=authoring) == (
        "machine-runner" if "agent-runner" in machine_identity.role() else "main"
    )
    assert run_git("branch", "--show-current", cwd=consolidated) == "main"
    assert (authoring / ".gitignore").is_file()
    assert (consolidated / ".gitignore").is_file()
    assert run_git("remote", cwd=authoring) == ""
    assert run_git("remote", cwd=consolidated) == ""
    assert (authoring / "MEMORY.md").is_file()


def test_gateway_only_does_not_require_a_machine_name() -> None:
    config = snapshot_process_config()
    config.set_field("machine_name", "")
    machine_identity = machine_from_boot(config)
    machine_identity.set(role="gateway")
    with pytest.raises(MachineNameMissing):
        machine_identity.name()
    scaffold(machine=machine_identity)
    assert run_git("branch", "--show-current", cwd=memory_dir()) == "main"


def test_keep_local_runner_creates_without_gateway_access(
    machine_identity: MachineIdentity,
) -> None:
    machine_identity.set(role="agent-runner", name="runner")
    set_field("memory_keep_local", True)
    set_field("gateway_url", "")
    scaffold(machine=machine_identity)
    root = memory_dir()
    assert run_git("branch", "--show-current", cwd=root) == "machine-runner"
    assert run_git("remote", cwd=root) == ""
    assert run_git("log", "-1", "--format=%s", cwd=root) == "init: local memory on machine-runner"


def test_keep_local_strips_all_remotes_and_switches_gateway_to_main(
    machine_identity: MachineIdentity,
) -> None:
    machine_identity.set(role="gateway", name="cloud")
    target = memory_dir()
    init_local_repo("main", target)
    run_git("checkout", "-b", "machine-cloud", cwd=target)
    run_git("remote", "add", "origin", "/unused-origin", cwd=target)
    run_git("remote", "add", "backup", "/unused-backup", cwd=target)
    set_field("memory_keep_local", True)
    scaffold(machine=machine_identity)
    assert run_git("branch", "--show-current", cwd=target) == "main"
    assert run_git("remote", cwd=target) == ""


def test_split_runner_without_remote_bootstraps_a_real_gateway_bundle(
    tmp_path: Path,
    machine_identity: MachineIdentity,
) -> None:
    machine_identity.set(role="agent-runner", name="runner")
    head, bundle = make_bundle(tmp_path / "source")
    response = PoolResponse(bundle, head=head)
    set_field("cluster_secret", "pool-secret")
    with pool_server(response) as url:
        set_field("gateway_url", url)
        scaffold(machine=machine_identity)
    root = memory_dir()
    assert run_git("rev-parse", "HEAD", cwd=root) == head
    assert run_git("branch", "--show-current", cwd=root) == "machine-runner"
    assert run_git("remote", cwd=root) == ""
    assert response.headers["Authorization"] == "Bearer pool-secret"
    assert (root / "health" / "a.md").read_text() == "note"
    assert (root / ".githooks" / "pre-commit").is_file()


def test_split_runner_bootstrap_failure_is_not_an_empty_pool(
    machine_identity: MachineIdentity,
) -> None:
    machine_identity.set(role="agent-runner", name="runner")
    with pool_server(PoolResponse(b"unavailable", status=502)) as url:
        set_field("gateway_url", url)
        with pytest.raises(MemoryRemoteMissing, match=r"gateway pool snapshot.*HTTP 502"):
            scaffold(machine=machine_identity)
    assert not (memory_dir() / ".git").exists()
    assert not (memory_dir() / "MEMORY.md").exists()


def test_scaffold_keeps_captured_target_and_next_invocation_recaptures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    machine_identity: MachineIdentity,
) -> None:
    machine_identity.set(role="agent-runner", name="runner")
    first = memory_dir()
    next_home = tmp_path / "next-home"
    head, bundle = make_bundle(tmp_path / "source")
    release = Event()
    response = PoolResponse(bundle, head=head, release=release)
    with pool_server(response) as url:
        set_field("gateway_url", url)
        # meiva:note
        # The actual HTTP exchange holds initialization while the caller changes
        # its home. Release before executor cleanup, even when an assertion fails.
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(scaffold, machine=machine_identity)
            try:
                assert response.received.wait(5), "bootstrap did not reach the real HTTP listener"
                point_home(monkeypatch, next_home)
            finally:
                release.set()
            pending.result(timeout=10)
        assert (first / ".githooks" / "pre-commit").is_file()
        assert not memory_dir().exists()
        scaffold(machine=machine_identity)
    assert memory_dir() != first
    assert (memory_dir() / ".githooks" / "pre-commit").is_file()


def _assert_stage_checkouts(
    authoring: Path,
    consolidated: Path,
    first_head: str,
    later_head: str,
    later_remote: Path,
    next_keep_local: bool,
) -> None:
    assert run_git("rev-parse", "HEAD", cwd=authoring) == first_head
    assert run_git("branch", "--show-current", cwd=authoring) == "machine-runner"
    assert (authoring / ".githooks" / "pre-commit").is_file()
    assert run_git("branch", "--show-current", cwd=consolidated) == "main"
    if next_keep_local:
        assert run_git("remote", cwd=consolidated) == ""
        assert run_git("log", "-1", "--format=%s", cwd=consolidated) == "init: local memory on main"
    else:
        assert run_git("remote", "get-url", "origin", cwd=consolidated) == str(later_remote)
        assert run_git("rev-parse", "HEAD", cwd=consolidated) == later_head
        assert "later gateway remote" in (consolidated / "note.md").read_text()


@pytest.mark.parametrize("next_keep_local", [True, False])
def test_combined_targets_and_gateway_stage_inputs_are_captured_at_their_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    machine_identity: MachineIdentity,
    next_keep_local: bool,
) -> None:
    machine_identity.set(role="gateway,agent-runner", name="runner")
    authoring = memory_dir()
    consolidated = gateway_memory_dir(roles=machine_identity.role())
    source = tmp_path / "initial-source"
    init_local_repo("main", source)
    first_head = commit_note(source, "initial remote")
    run_git("branch", "machine-runner", cwd=source)
    remote = tmp_path / "initial.git"
    run_git("clone", "--bare", str(source), str(remote), cwd=tmp_path)
    later_source = tmp_path / "later-source"
    init_local_repo("main", later_source)
    later_head = commit_note(later_source, "later gateway remote")
    later_remote = tmp_path / "later.git"
    run_git("clone", "--bare", str(later_source), str(later_remote), cwd=tmp_path)
    received, release = Event(), Event()
    with git_http_remote(remote, received=received, release=release) as url:
        set_field("memory_remote", url)
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(scaffold, machine=machine_identity)
            try:
                assert received.wait(5), "authoring clone did not reach Git HTTP discovery"
                point_home(monkeypatch, tmp_path / "next-home")
                set_field("memory_keep_local", next_keep_local)
                set_field("memory_remote", "" if next_keep_local else str(later_remote))
            finally:
                release.set()
            pending.result(timeout=15)
    _assert_stage_checkouts(
        authoring, consolidated, first_head, later_head, later_remote, next_keep_local
    )
    assert not memory_dir().exists()
    assert not gateway_memory_dir(roles=machine_identity.role()).exists()
    set_field("memory_keep_local", True)
    scaffold(machine=machine_identity)
    assert (memory_dir() / ".githooks" / "pre-commit").is_file()
    assert (gateway_memory_dir(roles=machine_identity.role()) / ".git").is_dir()


def test_existing_broken_checkout_preserves_the_git_exception(
    machine_identity: MachineIdentity,
) -> None:
    machine_identity.set(role="agent-runner", name="runner")
    (memory_dir() / ".git").mkdir(parents=True)
    with pytest.raises(subprocess.CalledProcessError):
        scaffold(machine=machine_identity)


def test_fresh_clone_git_failure_is_classified(
    machine_identity: MachineIdentity, tmp_path: Path
) -> None:
    machine_identity.set(role="agent-runner", name="runner")
    set_field("memory_remote", str(tmp_path / "not-a-repository"))
    with pytest.raises(RuntimeError, match="unrecognized failure shape") as failure:
        scaffold(machine=machine_identity)
    assert isinstance(failure.value.__cause__, subprocess.CalledProcessError)
