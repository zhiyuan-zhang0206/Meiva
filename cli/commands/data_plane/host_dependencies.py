# meiva:note
# Start-side host-package preflight: resolve the packages the selected roster
# executes, install a missing group through the repository's provisioners
# (Homebrew on macOS, apt on Debian/Ubuntu), re-probe, and refuse before
# converge when the install cannot complete.
# meiva:link cli/commands/lifecycle/docs/start-readiness.meiva.okf.md

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from base.cluster.dataplane.pg_tools import pg_tool
from base.deploy.progress_timeout import HOST_PACKAGE_INSTALL_TIMEOUT_S
from base.native_process.os_platform import is_macos
from base.sessions.env_forwarding import frontend_toolchain_path
from cli.commands.converge.spec import ConvergeCtx
from cli.commands.data_plane import cluster_instance as instance
from cli.commands.data_plane.config import DataPlaneConfig

_DATA_PLANE = "data-plane"
_NODE = "node"
_NODE_SERVICES = frozenset({"frontend", "browser", "browser-mcp"})

# meiva:note
# `pg_isready` is the first client tool only a host install carries; the
# vendored tree ships initdb/pg_ctl/postgres, so this probe decides whether a
# Linux gateway can start its own data plane.
_PG_TOOLS = ("initdb", "pg_ctl", "postgres", "pg_isready")


@dataclass(frozen=True)
class DependencyProblem:
    detail: str
    installable: bool = True


def postgres_problem(*, tool: Callable[[str], Path] = pg_tool) -> DependencyProblem | None:
    missing: list[str] = []
    for name in _PG_TOOLS:
        try:
            path = tool(name)
        except RuntimeError:
            missing.append(name)
            continue
        if not _executable(path):
            missing.append(name)
    if not missing:
        return None
    return DependencyProblem(f"PostgreSQL 17 tools missing: {', '.join(missing)}")


def redis_problem(
    config: DataPlaneConfig,
    *,
    server_bin: Callable[[DataPlaneConfig], str] = instance.redis_server_bin,
    cli_bin: Callable[[DataPlaneConfig], str] = instance.redis_cli_bin,
) -> DependencyProblem | None:
    try:
        server = server_bin(config)
        cli = cli_bin(config)
    except RuntimeError as exc:
        return DependencyProblem(f"Redis override is unusable: {exc}", installable=False)
    missing = [
        name
        for name, binary in (("redis-server", server), ("redis-cli", cli))
        if not _executable(binary)
    ]
    if not missing:
        return None
    return DependencyProblem(f"Redis missing: {', '.join(missing)}")


def _pgbouncer_bin() -> str:
    from base.cluster.dataplane import pooler

    return pooler.pgbouncer_bin()


def pgbouncer_problem(*, binary: Callable[[], str] = _pgbouncer_bin) -> DependencyProblem | None:
    resolved = binary()
    if _executable(resolved):
        return None
    return DependencyProblem(f"PgBouncer not found ({resolved!r})")


def _tool_which(name: str) -> str | None:
    return shutil.which(name, path=frontend_toolchain_path(os.environ.get("PATH", "")))


def node_problem(
    *,
    which: Callable[[str], str | None] = _tool_which,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> DependencyProblem | None:
    missing = [name for name in ("node", "npm", "npx") if which(name) is None]
    if missing:
        return DependencyProblem(f"Node.js toolchain missing: {', '.join(missing)}")
    node = which("node")
    if node is None:
        return DependencyProblem("Node.js toolchain missing: node")
    try:
        result = run(
            [node, "-p", "process.versions.node"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return DependencyProblem(f"Node.js version probe failed: {exc}")
    raw = result.stdout.strip()
    version = _node_version(raw)
    if version is None or version < (20, 9):
        return DependencyProblem(f"Node.js {raw or 'version unknown'} is older than 20.9")
    return None


def data_plane_problems(config: DataPlaneConfig) -> tuple[DependencyProblem, ...]:
    problems: list[DependencyProblem] = []
    for problem in (postgres_problem(), redis_problem(config)):
        if problem is not None:
            problems.append(problem)
    if config.pgbouncer_enabled:
        pooler_problem = pgbouncer_problem()
        if pooler_problem is not None:
            problems.append(pooler_problem)
    return tuple(problems)


def node_problems() -> tuple[DependencyProblem, ...]:
    problem = node_problem()
    return (problem,) if problem is not None else ()


@dataclass(frozen=True)
class HostDependencyProbes:
    data_plane: Callable[[DataPlaneConfig], tuple[DependencyProblem, ...]] = data_plane_problems
    node: Callable[[], tuple[DependencyProblem, ...]] = node_problems


def required_components(
    *,
    roles: frozenset[str] | None,
    services: frozenset[str],
    data_plane: DataPlaneConfig | None,
) -> tuple[str, ...]:
    required: list[str] = []
    if roles and "gateway" in roles and data_plane is not None and not data_plane.is_remote:
        required.append(_DATA_PLANE)
    if services & _NODE_SERVICES:
        required.append(_NODE)
    return tuple(required)


def ensure_host_dependencies(
    *,
    repo: Path,
    roles: frozenset[str] | None,
    services: frozenset[str],
    data_plane: DataPlaneConfig | None,
    probes: HostDependencyProbes | None = None,
    install: Callable[[Path, str], bool] | None = None,
    platform_is_macos: Callable[[], bool] | None = None,
) -> int:
    required = required_components(roles=roles, services=services, data_plane=data_plane)
    if not required:
        return 0
    active = probes or HostDependencyProbes()
    installer = install or _install
    missing = _problems(required, data_plane, probes=active)
    if not missing:
        return 0
    _report_missing(missing)
    for component in required:
        problems = missing.get(component, ())
        if any(problem.installable for problem in problems):
            installer(repo, component)
    remaining = _problems(required, data_plane, probes=active)
    if remaining:
        _report_refusal(remaining, repo=repo, macos=(platform_is_macos or is_macos)())
        return 1
    print("  ✓ host dependencies ready")
    return 0


def ensure_host_dependencies_step(
    ctx: ConvergeCtx, *, check: Callable[..., int] = ensure_host_dependencies
) -> None:
    from cli.commands.data_plane.config import data_plane_config_from_settings

    data_plane = data_plane_config_from_settings() if "gateway" in (ctx.roles or ()) else None
    if check(
        repo=ctx.repo,
        roles=ctx.roles,
        services=ctx.services,
        data_plane=data_plane,
    ):
        raise RuntimeError(
            "host dependencies are still incomplete; install the packages named above "
            "and retry `meiva start`"
        )


def _problems(
    components: tuple[str, ...],
    data_plane: DataPlaneConfig | None,
    *,
    probes: HostDependencyProbes,
) -> dict[str, tuple[DependencyProblem, ...]]:
    missing: dict[str, tuple[DependencyProblem, ...]] = {}
    for component in components:
        if component == _NODE:
            found = probes.node()
        else:
            found = probes.data_plane(data_plane) if data_plane is not None else ()
        if found:
            missing[component] = found
    return missing


def _report_missing(missing: dict[str, tuple[DependencyProblem, ...]]) -> None:
    print("\n→ host dependencies")
    for problems in missing.values():
        for problem in problems:
            print(f"  · {problem.detail}")


def _install(repo: Path, component: str) -> bool:
    script = repo / "scripts" / "provision" / _PROVISION_SCRIPTS[component]
    if not script.is_file():
        print(f"  ✗ provision script missing: {script}", file=sys.stderr)
        return False
    print(
        f"  · installing {_component_label(component)} with {_package_manager()} "
        "(this can take several minutes)"
    )
    try:
        result = subprocess.run(
            ["bash", str(script)],
            cwd=repo,
            check=False,
            timeout=HOST_PACKAGE_INSTALL_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  ✗ {script.name} failed to run: {exc}", file=sys.stderr)
        return False
    if result.returncode != 0:
        print(
            f"  ! {script.name} exited {result.returncode}; re-checking what landed",
            file=sys.stderr,
        )
    return True


_PROVISION_SCRIPTS = {_DATA_PLANE: "database.sh", _NODE: "node.sh"}


def _component_label(component: str) -> str:
    return "PostgreSQL/Redis/PgBouncer" if component == _DATA_PLANE else "Node.js"


def _package_manager() -> str:
    return "Homebrew" if is_macos() else "apt"


def _report_refusal(
    missing: dict[str, tuple[DependencyProblem, ...]], *, repo: Path, macos: bool
) -> None:
    print(
        "\n✗ host dependency preflight failed: `meiva start` launched nothing.",
        file=sys.stderr,
    )
    for problems in missing.values():
        for problem in problems:
            print(f"    ✗ {problem.detail}", file=sys.stderr)
    print("\n  Install the missing packages, then retry `meiva start`:", file=sys.stderr)
    for line in _remediation(missing, repo=repo, macos=macos):
        print(f"    {line}", file=sys.stderr)


def _remediation(
    missing: dict[str, tuple[DependencyProblem, ...]], *, repo: Path, macos: bool
) -> list[str]:
    lines: list[str] = []
    wants_data_plane = _DATA_PLANE in missing
    wants_node = _NODE in missing
    if macos:
        lines.append("macOS needs Homebrew first: https://brew.sh")
        if wants_data_plane:
            lines.append(
                "macOS (Homebrew): brew install postgresql@17 redis@8.2 pgbouncer pgvector"
            )
        if wants_node:
            lines.append("macOS (Homebrew): brew install node")
    else:
        if wants_data_plane:
            lines.append(f"Debian/Ubuntu: sudo bash {repo / 'scripts/provision/database.sh'}")
        if wants_node:
            lines.append(f"Debian/Ubuntu: sudo bash {repo / 'scripts/provision/node.sh'}")
        lines.append(
            "Other Linux: install PostgreSQL 17 (with client tools), Redis 8.2, "
            "PgBouncer and Node 22 with your package manager"
        )
        lines.append("No sudo on this host? run the provision scripts as root")
    if any(not problem.installable for problems in missing.values() for problem in problems):
        lines.append(
            "Fix MEIVA_REDIS_BIN_DIR: it must contain executable redis-server and redis-cli"
        )
    return lines


def _executable(binary: str | Path) -> bool:
    text = str(binary)
    if os.sep in text:
        path = Path(text)
        return path.is_file() and os.access(path, os.X_OK)
    return shutil.which(text) is not None


def _node_version(raw: str) -> tuple[int, int] | None:
    parts = raw.split(".")
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None
