from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import cli.commands.data_plane.host_dependencies as hd
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import DARWIN_ARM64, LINUX_X86
from cli.commands.data_plane.config import DataPlaneConfig

FOREIGN_DB = "postgresql://meiva:pw@10.9.8.7:5432/meiva"
FOREIGN_REDIS = "redis://meiva:pw@10.9.8.7:6380/0"
GATEWAY = frozenset({"gateway"})
RUNNER = frozenset({"agent-runner"})
FULL_ROSTER = frozenset({"gateway", "frontend", "ops", "agent-host"})
FRONTEND_ONLY = frozenset({"frontend"})
HEADLESS = frozenset({"gateway"})
_DEFAULT = object()

# meiva:note
# The systemctl probe reads the real `/run/systemd/system`, which the fake `Host` cannot supply, so a start that requires the boot unit only passes on a host that is running systemd.
requires_systemd = pytest.mark.skipif(
    not Path("/run/systemd/system").is_dir(),
    reason="the systemctl probe reads the real /run/systemd/system",
)


# meiva:note
# A test-only data-plane config; these credentials never leave the fake value.
def _config(
    *,
    db_url: str = "postgresql://meiva:pw@127.0.0.1:5432/meiva",
    redis_url: str = "redis://meiva:pw@127.0.0.1:6380/0",
    pgbouncer: bool = True,
) -> DataPlaneConfig:
    return DataPlaneConfig(
        db_url=db_url,
        redis_url=redis_url,
        redis_admin_password="admin",  # noqa: S106
        cluster_secret="secret",  # noqa: S106
        pgbouncer_enabled=pgbouncer,
        pooler_max_client_conn=100,
        redis_bin_dir="",
        trusted_cidrs="",
        events_channel="meiva:events",
        memory_search_backend="pgvector",
        embedding_backend="openai",
    )


def _no_install(_repo: Path, _component: str) -> None:
    return


def _run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, "22.14.0\n", "")


# meiva:note
# A host where every tool resolves unless `present[name]` is False. The mapping is
# live, so an install that lands a tool is visible to the next probe.
def _host(
    present: dict[str, bool] | None = None,
    *,
    macos: bool = False,
    uid: int = 1000,
) -> Host:
    known: dict[str, bool] = present if present is not None else {}

    def which(name: str) -> str | None:
        return None if known.get(name) is False else f"/usr/bin/{name}"

    return Host(
        run=_run,
        which=which,
        platform=DARWIN_ARM64 if macos else LINUX_X86,
        uid=uid,
    )


def _scope(
    *,
    roles: frozenset[str] | None = GATEWAY,
    services: frozenset[str] = FULL_ROSTER,
    data_plane: DataPlaneConfig | object | None = _DEFAULT,
    host: Host | None = None,
    os_jobs: bool = True,
    permissions_helper: bool = False,
    require_github_pr: bool = False,
    memory_keep_local: bool = False,
    prod_install: bool = True,
) -> hd.StartScope:
    plane = _config() if data_plane is _DEFAULT else data_plane
    return hd.StartScope(
        roles=roles,
        services=services,
        # meiva:note
        # `_DEFAULT` keeps "no local data plane" (`None`) distinguishable from "use the fixture".
        data_plane=plane,  # pyright: ignore[reportArgumentType]
        host=host or _host(),
        os_jobs=os_jobs,
        permissions_helper=permissions_helper,
        require_github_pr=require_github_pr,
        memory_keep_local=memory_keep_local,
        prod_install=prod_install,
    )


# meiva:note
# A probes set where every binary resolves; each test overrides only what it removes.
class _DataPlane:
    def __init__(
        self,
        *,
        missing_postgres: tuple[str, ...] = (),
        missing_redis: tuple[str, ...] = (),
        pgbouncer: str = sys.executable,
        node: hd.DependencyProblem | None = None,
        redis: Any = None,
    ) -> None:
        self.gone = set(missing_postgres)
        self.redis = redis
        self._missing_redis = set(missing_redis)
        self._pgbouncer = pgbouncer
        self._node = node

    def postgres(self, name: str) -> Path:
        if name in self.gone:
            raise RuntimeError(f"{name} not found")
        return Path(sys.executable)

    def redis_server(self, config: DataPlaneConfig) -> str:
        if self.redis is not None:
            return self.redis(config)
        return "/missing/redis-server" if "redis-server" in self._missing_redis else sys.executable

    def redis_cli(self, config: DataPlaneConfig) -> str:
        if self.redis is not None:
            return self.redis(config)
        return "/missing/redis-cli" if "redis-cli" in self._missing_redis else sys.executable

    def pgbouncer(self) -> str:
        return self._pgbouncer

    def node(self, _host: Host, _name: str) -> hd.DependencyProblem | None:
        return self._node

    def probes(self) -> hd.HostDependencyProbes:
        return hd.HostDependencyProbes(
            postgres=self.postgres,
            redis_server=self.redis_server,
            redis_cli=self.redis_cli,
            pgbouncer=self.pgbouncer,
            node=self.node,
        )


def _names(scope: hd.StartScope) -> set[str]:
    return {tool.name for tool in hd.required_tools(scope)}


def test_gateway_with_the_frontend_requires_the_full_table() -> None:
    assert _names(_scope()) == {
        "initdb",
        "pg_ctl",
        "postgres",
        "pg_isready",
        "redis-server",
        "redis-cli",
        "pgbouncer",
        "node",
        "npm",
        "npx",
        "crontab",
        "systemctl",
        "sudo",
        "curl",
        "sh",
        "git",
    }


def test_headless_gateway_needs_no_frontend_tools() -> None:
    assert _names(_scope(services=HEADLESS)).isdisjoint({"node", "npm", "npx", "curl"})


def test_runner_only_host_skips_the_local_data_plane() -> None:
    names = _names(_scope(roles=RUNNER, data_plane=None, services=FRONTEND_ONLY))
    assert names.isdisjoint(
        {"postgres", "pg_ctl", "initdb", "pg_isready", "redis-server", "redis-cli", "pgbouncer"}
    )
    assert {"node", "crontab", "systemctl", "sh"} <= names


def test_remote_plane_skips_the_local_data_plane() -> None:
    remote = _config(db_url=FOREIGN_DB, redis_url=FOREIGN_REDIS)
    assert _names(_scope(data_plane=remote)).isdisjoint({"redis-server", "pgbouncer", "postgres"})


def test_os_jobs_off_skips_the_scheduler_rows() -> None:
    assert _names(_scope(os_jobs=False)).isdisjoint({"crontab", "systemctl", "sudo", "launchctl"})


def test_pooling_off_drops_pgbouncer() -> None:
    assert "pgbouncer" not in _names(_scope(data_plane=_config(pgbouncer=False)))


def test_macos_uses_launchctl_and_the_helper_tools() -> None:
    names = _names(_scope(host=_host(macos=True), permissions_helper=True))
    assert {"launchctl", "swiftc", "codesign"} <= names
    assert names.isdisjoint({"crontab", "systemctl", "sudo"})


def test_root_does_not_require_sudo() -> None:
    assert "sudo" not in _names(_scope(host=_host(uid=0)))


def test_git_is_required_on_every_start() -> None:
    selections: tuple[tuple[frozenset[str] | None, frozenset[str]], ...] = (
        (GATEWAY, HEADLESS),
        (RUNNER, frozenset()),
        (None, frozenset()),
    )
    for roles, services in selections:
        scope = _scope(roles=roles, services=services, data_plane=None)
        assert "git" in _names(scope)


def test_a_split_runner_consolidating_by_pr_needs_the_github_cli() -> None:
    scope = _scope(roles=RUNNER, data_plane=None, require_github_pr=True)
    assert "gh" in _names(scope)


def test_a_gateway_never_needs_the_github_cli() -> None:
    assert "gh" not in _names(_scope(require_github_pr=True))


def test_a_local_memory_pool_or_an_explicit_opt_out_drops_the_github_cli() -> None:
    local = _scope(roles=RUNNER, data_plane=None, require_github_pr=True, memory_keep_local=True)
    opted_out = _scope(roles=RUNNER, data_plane=None, require_github_pr=False)
    assert "gh" not in _names(local)
    assert "gh" not in _names(opted_out)


def test_a_dev_worktree_start_does_not_require_the_boot_unit() -> None:
    scope = _scope(prod_install=False)
    names = _names(scope)
    assert "systemctl" not in names
    assert "sudo" not in names
    assert {"crontab", "git"} <= names


def test_optional_tools_are_never_required() -> None:
    assert {tool.name for tool in hd.HOST_TOOLS if tool.optional} == {"brew", "lsof", "ps"}
    assert all(not tool.optional for tool in hd.required_tools(_scope()))


@requires_systemd
def test_all_present_is_silent(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert (
        hd.ensure_host_dependencies(
            repo=tmp_path,
            scope=_scope(),
            probes=_DataPlane().probes(),
            install=lambda _repo, _component: pytest.fail("unexpected install"),
        )
        == 0
    )
    assert capsys.readouterr().out == ""


@requires_systemd
def test_missing_crontab_installs_host_utils_then_passes(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    present = {"crontab": False}
    installed: list[str] = []

    def install(_repo: Path, component: str) -> None:
        installed.append(component)
        present["crontab"] = True

    scope = _scope(host=_host(present))
    assert (
        hd.ensure_host_dependencies(
            repo=tmp_path, scope=scope, probes=_DataPlane().probes(), install=install
        )
        == 0
    )
    assert installed == ["host-utils"]
    assert "crontab" in capsys.readouterr().out


@requires_systemd
def test_a_second_start_does_not_reinstall(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    present = {"crontab": False}
    installed: list[str] = []

    def install(_repo: Path, component: str) -> None:
        installed.append(component)
        present["crontab"] = True

    scope = _scope(host=_host(present))
    assert (
        hd.ensure_host_dependencies(
            repo=tmp_path, scope=scope, probes=_DataPlane().probes(), install=install
        )
        == 0
    )
    # meiva:note
    # Discard the first run's report: the second run must be silent.
    capsys.readouterr()
    assert (
        hd.ensure_host_dependencies(
            repo=tmp_path, scope=scope, probes=_DataPlane().probes(), install=install
        )
        == 0
    )
    assert installed == ["host-utils"]
    assert "host dependencies" not in capsys.readouterr().out


# meiva:note
# A host-owned tool cannot be fixed by any provisioner, so the gate must not spend
# minutes installing the groups that could have been fixed before it refuses.
def test_a_host_owned_refusal_skips_the_installers(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    installs: list[str] = []

    def install(_repo: Path, component: str) -> None:
        installs.append(component)

    assert (
        hd.ensure_host_dependencies(
            repo=tmp_path,
            scope=_scope(host=_host({"crontab": False, "systemctl": False})),
            probes=_DataPlane().probes(),
            install=install,
        )
        == 1
    )
    assert installs == []
    err = capsys.readouterr().err
    assert "✗ crontab" in err and "✗ systemctl" in err


def test_missing_crontab_that_cannot_be_installed_names_the_tool_and_the_command(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert (
        hd.ensure_host_dependencies(
            repo=tmp_path,
            scope=_scope(host=_host({"crontab": False})),
            probes=_DataPlane().probes(),
            install=_no_install,
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "launched nothing" in err
    assert "✗ crontab" in err
    assert str(tmp_path / "scripts/provision/host_utils.sh") in err


def test_a_host_owned_tool_refuses_with_the_escape_command(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    installs: list[str] = []

    def install(_repo: Path, component: str) -> None:
        installs.append(component)

    assert (
        hd.ensure_host_dependencies(
            repo=tmp_path,
            scope=_scope(host=_host({"systemctl": False})),
            probes=_DataPlane().probes(),
            install=install,
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "✗ systemctl" in err
    assert "MEIVA_OS_JOBS_ENABLED=0" in err
    assert installs == []


@requires_systemd
def test_missing_postgres_installs_the_database_group(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    probe = _DataPlane(missing_postgres=("initdb", "pg_ctl", "postgres", "pg_isready"))
    installed: list[str] = []

    def install(_repo: Path, component: str) -> None:
        installed.append(component)
        probe.gone.clear()

    assert (
        hd.ensure_host_dependencies(
            repo=tmp_path, scope=_scope(), probes=probe.probes(), install=install
        )
        == 0
    )
    assert installed == ["data-plane"]
    assert "initdb" in capsys.readouterr().out


def test_node_older_than_the_floor_refuses(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    problem = hd.DependencyProblem("18.19.1 is older than 20.9")
    scope = _scope(roles=RUNNER, data_plane=None, services=FRONTEND_ONLY)
    assert (
        hd.ensure_host_dependencies(
            repo=tmp_path,
            scope=scope,
            probes=_DataPlane(node=problem).probes(),
            install=_no_install,
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "18.19.1 is older than 20.9" in err


def test_unusable_redis_override_is_not_installable(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    def override(_config: DataPlaneConfig) -> str:
        raise RuntimeError("MEIVA_REDIS_BIN_DIR requires an executable file")

    assert (
        hd.ensure_host_dependencies(
            repo=tmp_path,
            scope=_scope(services=HEADLESS),
            probes=_DataPlane(redis=override).probes(),
            install=lambda _repo, _component: pytest.fail("unexpected install"),
        )
        == 1
    )
    assert "MEIVA_REDIS_BIN_DIR" in capsys.readouterr().err


def test_mac_tool_refuses_with_xcode_select(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    scope = _scope(
        roles=RUNNER,
        data_plane=None,
        services=HEADLESS,
        host=_host({"codesign": False}, macos=True),
        os_jobs=False,
        permissions_helper=True,
    )
    assert (
        hd.ensure_host_dependencies(
            repo=tmp_path,
            scope=scope,
            probes=_DataPlane().probes(),
            install=lambda _repo, _component: pytest.fail("unexpected install"),
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "✗ codesign" in err
    assert "xcode-select --install" in err


def test_node_tool_problem_reports_the_missing_binary() -> None:
    problem = hd.node_tool_problem(
        "npm", which=lambda name: None if name == "npm" else "/usr/bin/" + name
    )
    assert problem is not None
    assert "npm" in problem.detail


# meiva:note
# The production runner is `Host.run` (`base.host.proc.run_bounded`): it bounds the
# whole process tree and, by design, has no `check=` keyword. A probe that passes
# `subprocess.run`'s `check` would raise TypeError on every real host.
def test_node_probe_uses_the_host_runner_contract() -> None:
    def run(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "check" in kwargs:
            raise TypeError("Popen.__init__() got an unexpected keyword argument 'check'")
        return subprocess.CompletedProcess([], 0, "22.14.0\n", "")

    assert hd.node_tool_problem("node", which=lambda name: "/usr/bin/" + name, run=run) is None


def test_node_tool_problem_accepts_22() -> None:
    assert hd.node_tool_problem("node", which=lambda name: "/usr/bin/" + name, run=_run) is None


def test_node_tool_problem_rejects_the_ubuntu_18_default() -> None:
    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, "18.19.1\n", "")

    problem = hd.node_tool_problem("node", which=lambda name: "/usr/bin/" + name, run=run)
    assert problem is not None
    assert "older than 20.9" in problem.detail


@requires_systemd
def test_the_postgres_probe_names_only_the_missing_tool() -> None:
    probes = _DataPlane(missing_postgres=("pg_isready",)).probes()
    missing = hd.missing_tools(_scope(), probes=probes)
    named = {tool.name: problem.detail for tool, problem in missing}
    assert set(named) == {"pg_isready"}
    assert "vendored" in named["pg_isready"]


@requires_systemd
def test_the_pgbouncer_probe_reports_the_resolved_path() -> None:
    probes = _DataPlane(pgbouncer="/missing/pgbouncer").probes()
    missing = hd.missing_tools(_scope(), probes=probes)
    assert [tool.name for tool, _problem in missing] == ["pgbouncer"]
    assert "/missing/pgbouncer" in missing[0][1].detail


# meiva:note
# The Settings shape the step reads, built without constructing Settings; the
# credentials are fake values that never leave this module.
def _view(
    *, permissions_helper: bool = False, os_jobs: bool = True, github_pr: bool = True
) -> SimpleNamespace:
    plane = SimpleNamespace(
        db_url="postgresql://meiva:pw@127.0.0.1:5432/meiva",
        redis_url="redis://meiva:pw@127.0.0.1:6380/0",
        redis_admin_password="admin",  # noqa: S106
        cluster_secret="secret",  # noqa: S106
        pgbouncer_enabled=True,
        pooler_max_client_conn=100,
        redis_bin_dir="",
        trusted_cidrs="",
        events_channel="meiva:events",
    )
    return SimpleNamespace(
        data_plane=plane,
        services=SimpleNamespace(
            memory_search_backend="pgvector",
            embedding_backend="openai",
            permissions_helper_enabled=permissions_helper,
        ),
        general=SimpleNamespace(
            os_jobs_enabled=os_jobs,
            require_github_pr=github_pr,
            memory_keep_local=False,
        ),
    )


class _Ctx:
    # meiva:note
    # A duck-typed ConvergeCtx: the step reads only these fields. The home is the
    # default one so `prod_install` is decided by the checkout path alone, while
    # the process's own MEIVA_HOME (the test harness's temporary home) is what
    # `owns_os_jobs` reads — the pair the real step sees.
    repo = Path("/repo")
    meiva_home = Path.home() / ".meiva"
    roles = RUNNER
    services = frozenset({"frontend"})
    host = _host()

    def read_config(self) -> Any:
        return SimpleNamespace(view=_view())


def test_step_refuses_when_the_check_fails() -> None:
    calls: list[dict[str, object]] = []

    def check(**kwargs: object) -> int:
        calls.append(kwargs)
        return 1

    # meiva:note
    # `_Ctx` is a duck-typed ConvergeCtx; pyright cannot see that it satisfies the protocol.
    with pytest.raises(RuntimeError, match="refused this start"):
        hd.ensure_host_dependencies_step(_Ctx(), check=check)  # pyright: ignore[reportArgumentType]
    assert calls and calls[0]["repo"] == Path("/repo")
    scope = calls[0]["scope"]
    assert isinstance(scope, hd.StartScope)
    assert scope.roles == RUNNER
    assert "node" in _names(scope)
    # meiva:note
    # The two gates the step derives rather than hardcodes: OS jobs need the default
    # home this process does not own, and the view's helper flag reaches the scope.
    assert scope.os_jobs is False
    assert scope.prod_install is True
    assert scope.permissions_helper is False


def test_step_passes_the_helper_flag_and_the_os_jobs_switch_through() -> None:
    seen: list[hd.StartScope] = []

    class _Incapable(_Ctx):
        def read_config(self) -> Any:
            return SimpleNamespace(view=_view(permissions_helper=True, os_jobs=False))

    def check(**kwargs: object) -> int:
        scope = kwargs["scope"]
        assert isinstance(scope, hd.StartScope)
        seen.append(scope)
        return 0

    # meiva:note
    # `_Incapable` is a duck-typed ConvergeCtx; pyright cannot see the protocol match.
    hd.ensure_host_dependencies_step(_Incapable(), check=check)  # pyright: ignore[reportArgumentType]
    assert seen[0].permissions_helper is True
    assert seen[0].os_jobs is False
