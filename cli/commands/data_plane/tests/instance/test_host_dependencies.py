from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import cli.commands.data_plane.host_dependencies as hd
from cli.commands.data_plane.config import DataPlaneConfig

FOREIGN_DB = "postgresql://meiva:pw@10.9.8.7:5432/meiva"
FOREIGN_REDIS = "redis://meiva:pw@10.9.8.7:6380/0"
GATEWAY = frozenset({"gateway"})
RUNNER = frozenset({"agent-runner"})
FULL_ROSTER = frozenset({"gateway", "frontend", "ops", "agent-host"})
NO_PROBLEMS = hd.HostDependencyProbes(data_plane=lambda _config: (), node=lambda: ())


# meiva:note
# A test-only data-plane config; these credentials never leave the fake value.
def _config(
    *,
    db_url: str = "postgresql://meiva:pw@127.0.0.1:5432/meiva",
    redis_url: str = "redis://meiva:pw@127.0.0.1:6380/0",
) -> DataPlaneConfig:
    return DataPlaneConfig(
        db_url=db_url,
        redis_url=redis_url,
        redis_admin_password="admin",  # noqa: S106
        cluster_secret="secret",  # noqa: S106
        pgbouncer_enabled=True,
        pooler_max_client_conn=100,
        redis_bin_dir="",
        trusted_cidrs="",
        events_channel="meiva:events",
        memory_search_backend="pgvector",
        embedding_backend="openai",
    )


def _problem(detail: str, *, installable: bool = True) -> hd.DependencyProblem:
    return hd.DependencyProblem(detail, installable=installable)


def test_gateway_roster_requires_data_plane_and_node() -> None:
    assert hd.required_components(roles=GATEWAY, services=FULL_ROSTER, data_plane=_config()) == (
        "data-plane",
        "node",
    )


def test_headless_gateway_roster_requires_only_the_data_plane() -> None:
    assert hd.required_components(
        roles=GATEWAY, services=frozenset({"gateway"}), data_plane=_config()
    ) == ("data-plane",)


def test_runner_only_host_has_no_local_data_plane() -> None:
    assert hd.required_components(roles=RUNNER, services=frozenset(), data_plane=None) == ()


def test_runner_browser_still_requires_node() -> None:
    assert hd.required_components(
        roles=RUNNER, services=frozenset({"browser"}), data_plane=None
    ) == ("node",)


def test_remote_plane_skips_local_data_plane() -> None:
    assert hd.required_components(
        roles=GATEWAY,
        services=frozenset({"gateway", "frontend"}),
        data_plane=_config(db_url=FOREIGN_DB, redis_url=FOREIGN_REDIS),
    ) == ("node",)


def test_all_present_is_silent(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert (
        hd.ensure_host_dependencies(
            repo=tmp_path,
            roles=RUNNER,
            services=frozenset({"frontend"}),
            data_plane=None,
            probes=NO_PROBLEMS,
            install=lambda _repo, _component: pytest.fail("unexpected install"),
        )
        == 0
    )
    assert capsys.readouterr().out == ""


def test_installs_each_missing_group_then_rechecks(
    tmp_path: Path,
) -> None:
    missing: dict[str, tuple[hd.DependencyProblem, ...]] = {
        "data-plane": (_problem("redis missing"),),
        "node": (_problem("node missing"),),
    }
    installed: list[str] = []
    probes = hd.HostDependencyProbes(
        data_plane=lambda _config: missing.get("data-plane", ()),
        node=lambda: missing.get("node", ()),
    )

    def install(_repo: Path, component: str) -> bool:
        installed.append(component)
        missing.pop(component)
        return True

    assert (
        hd.ensure_host_dependencies(
            repo=tmp_path,
            roles=GATEWAY,
            services=FULL_ROSTER,
            data_plane=_config(),
            probes=probes,
            install=install,
        )
        == 0
    )
    assert installed == ["data-plane", "node"]


def test_install_failure_refuses_with_the_brew_commands(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    probes = hd.HostDependencyProbes(
        data_plane=lambda _config: (_problem("redis missing"),),
        node=lambda: (_problem("node missing"),),
    )
    assert (
        hd.ensure_host_dependencies(
            repo=tmp_path,
            roles=GATEWAY,
            services=FULL_ROSTER,
            data_plane=_config(),
            probes=probes,
            install=lambda _repo, _component: False,
            platform_is_macos=lambda: True,
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "launched nothing" in err
    assert "brew install postgresql@17 redis@8.2 pgbouncer pgvector" in err
    assert "brew install node" in err


def test_install_failure_refuses_with_the_apt_provisioners(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    probes = hd.HostDependencyProbes(
        data_plane=lambda _config: (_problem("redis missing"),),
        node=lambda: (_problem("node missing"),),
    )
    assert (
        hd.ensure_host_dependencies(
            repo=tmp_path,
            roles=GATEWAY,
            services=FULL_ROSTER,
            data_plane=_config(),
            probes=probes,
            install=lambda _repo, _component: False,
            platform_is_macos=lambda: False,
        )
        == 1
    )
    err = capsys.readouterr().err
    assert str(tmp_path / "scripts/provision/database.sh") in err
    assert str(tmp_path / "scripts/provision/node.sh") in err


def test_unusable_redis_override_is_not_installable(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    probes = hd.HostDependencyProbes(
        data_plane=lambda _config: (_problem("Redis override is unusable", installable=False),),
        node=lambda: (),
    )
    assert (
        hd.ensure_host_dependencies(
            repo=tmp_path,
            roles=GATEWAY,
            services=frozenset({"gateway"}),
            data_plane=_config(),
            probes=probes,
            install=lambda _repo, _component: pytest.fail("unexpected install"),
            platform_is_macos=lambda: False,
        )
        == 1
    )
    assert "MEIVA_REDIS_BIN_DIR" in capsys.readouterr().err


def test_node_problem_reports_the_missing_tool() -> None:
    def which(name: str) -> str | None:
        return None if name == "npm" else f"/usr/bin/{name}"

    problem = hd.node_problem(which=which)
    assert problem is not None
    assert "npm" in problem.detail


def test_node_problem_rejects_the_ubuntu_18_default() -> None:
    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, "18.19.1\n", "")

    problem = hd.node_problem(which=lambda name: f"/usr/bin/{name}", run=run)
    assert problem is not None
    assert "older than 20.9" in problem.detail


def test_node_problem_accepts_22() -> None:
    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, "22.14.0\n", "")

    assert hd.node_problem(which=lambda name: f"/usr/bin/{name}", run=run) is None


def test_postgres_problem_names_only_the_missing_tool() -> None:
    def tool(name: str) -> Path:
        if name == "pg_isready":
            raise RuntimeError("not found")
        return Path(sys.executable)

    problem = hd.postgres_problem(tool=tool)
    assert problem is not None
    assert "pg_isready" in problem.detail
    assert "initdb" not in problem.detail


def test_redis_override_failure_is_marked_non_installable() -> None:
    def unavailable(_config: DataPlaneConfig) -> str:
        raise RuntimeError("MEIVA_REDIS_BIN_DIR requires an executable file")

    problem = hd.redis_problem(_config(), server_bin=unavailable)
    assert problem is not None
    assert problem.installable is False


def test_pgbouncer_problem_reports_the_resolved_path() -> None:
    problem = hd.pgbouncer_problem(binary=lambda: "/missing/pgbouncer")
    assert problem is not None
    assert "/missing/pgbouncer" in problem.detail


class _Ctx:
    repo = Path("/repo")
    roles = RUNNER
    services = frozenset({"frontend"})


def test_host_dependency_step_refuses_when_the_check_fails() -> None:
    calls: list[dict[str, object]] = []

    def check(**kwargs: object) -> int:
        calls.append(kwargs)
        return 1

    # meiva:note
    # _Ctx is a duck-typed ConvergeCtx: the step reads only repo, roles and services.
    with pytest.raises(RuntimeError, match="still incomplete"):
        hd.ensure_host_dependencies_step(_Ctx(), check=check)  # pyright: ignore[reportArgumentType]
    assert calls == [
        {
            "repo": Path("/repo"),
            "roles": RUNNER,
            "services": frozenset({"frontend"}),
            "data_plane": None,
        }
    ]
