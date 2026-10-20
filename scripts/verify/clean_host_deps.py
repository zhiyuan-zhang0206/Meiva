#!/usr/bin/env python3
# meiva:note
# The clean-host recipe births a container with the base utilities a stock
# Ubuntu server image carries (Python, git, sudo, cron) and none of the packages
# under test, runs uv sync + meiva init + the first meiva start for a
# gateway+frontend roster, proves those packages were absent before and present
# after, then restarts gateway-only to prove a ready boot. It is a one-off
# verification recipe, not a CI gate: packages come from the network and the run
# takes several minutes.

from __future__ import annotations

import argparse
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROFILE = """\
MEIVA_OS_JOBS_ENABLED=0
MEIVA_PROVISION_BUILTIN_SCHEDULES=0
MEIVA_BROWSER_ENABLED=0
MEIVA_MEMORY_KEEP_LOCAL=1
MEIVA_CROSS_MACHINE_TRANSFER_BACKEND=none
MEIVA_REQUIRE_GITHUB_PR=0
MEIVA_TELEMETRY_OTLP_ENABLED=0
MEIVA_TIMEZONE=UTC
"""
_TOOLS = ("redis-server", "pgbouncer", "node", "npm", "npx")


class CleanHostDepsError(RuntimeError):
    pass


def docker_argv(command: str = "docker") -> list[str]:
    return shlex.split(command)


def docker_available(docker: list[str]) -> bool:
    return bool(docker) and (shutil.which(docker[0]) is not None or Path(docker[0]).exists())


# meiva:note
# argv is assembled here from operator-supplied executable names and fixed
# arguments; no shell is involved.
def run(
    argv: list[str], *, timeout: float = 600.0, check: bool = True
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(  # noqa: S603
        argv, capture_output=True, text=True, timeout=timeout, check=False
    )
    if check and result.returncode != 0:
        raise CleanHostDepsError(
            f"command failed ({result.returncode}): {shlex.join(argv)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def docker_exec_result(
    container: str,
    script: str,
    *,
    docker: list[str],
    user: str = "verify",
    timeout: float = 600.0,
) -> subprocess.CompletedProcess[str]:
    return run(
        [*docker, "exec", "-u", user, container, "bash", "-lc", script],
        timeout=timeout,
        check=False,
    )


def docker_exec(
    container: str,
    script: str,
    *,
    docker: list[str],
    user: str = "verify",
    timeout: float = 600.0,
) -> str:
    result = docker_exec_result(container, script, docker=docker, user=user, timeout=timeout)
    if result.returncode != 0:
        raise CleanHostDepsError(
            f"container step failed ({result.returncode}): {script}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout


# meiva:note
# git archive is a fixed argv with the operator-supplied ref; the tar stream
# goes straight to a file so the binary payload is never captured as text.
def git_archive(ref: str, target: Path) -> None:
    with target.open("wb") as handle:
        result = subprocess.run(  # noqa: S603
            ["git", "-C", str(REPO), "archive", "--format=tar", ref],
            stdout=handle,
            stderr=subprocess.PIPE,
            check=False,
        )
    if result.returncode != 0:
        raise CleanHostDepsError(
            f"git archive {ref} failed: {result.stderr.decode(errors='replace')}"
        )


def base_toolchain(container: str, *, docker: list[str]) -> None:
    docker_exec(
        container,
        "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
        "--no-install-recommends python3.12 python3.12-venv python3.12-dev "
        "build-essential ca-certificates curl git sudo locales cron >/dev/null "
        "&& locale-gen en_US.UTF-8 >/dev/null "
        "&& useradd --create-home --shell /bin/bash verify "
        "&& echo 'verify ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/verify "
        "&& chmod 440 /etc/sudoers.d/verify",
        docker=docker,
        user="root",
        timeout=900,
    )


def copy_source(container: str, source: Path, profile: Path, *, docker: list[str]) -> None:
    uv = shutil.which("uv")
    if uv is None:
        raise CleanHostDepsError("uv is not on PATH — the container needs the host's uv binary")
    run([*docker, "cp", str(Path(uv).resolve()), f"{container}:/usr/local/bin/uv"], timeout=60)
    docker_exec(container, "chmod +x /usr/local/bin/uv", docker=docker, user="root")
    docker_exec(container, "mkdir -p /home/verify/.meiva/source", docker=docker)
    run([*docker, "cp", str(source), f"{container}:/tmp/source.tar"], timeout=120)
    run([*docker, "cp", str(profile), f"{container}:/tmp/profile.env"], timeout=60)
    docker_exec(
        container,
        "tar -xf /tmp/source.tar -C /home/verify/.meiva/source && chown -R verify:verify /home/verify/.meiva",
        docker=docker,
        user="root",
    )
    # meiva:note
    # The migrator only applies files git tracks and refuses a bare directory; a
    # synthetic commit gives the archive the worktree shape a real git clone has.
    docker_exec(
        container,
        "cd /home/verify/.meiva/source && git init -q && git add -A "
        "&& git -c user.email=verify@example.invalid -c user.name=verify "
        "commit -qm 'clean-host-deps archive' && git rev-parse HEAD",
        docker=docker,
    )


def tool_presence(container: str, *, docker: list[str]) -> str:
    checks = "; ".join(
        f"command -v {tool} >/dev/null 2>&1 && echo PRESENT:{tool} || echo ABSENT:{tool}"
        for tool in _TOOLS
    )
    return docker_exec(
        container,
        f"{checks}; "
        "[ -x /usr/lib/postgresql/17/bin/initdb ] && echo PRESENT:postgres || echo ABSENT:postgres",
        docker=docker,
    )


def init_cluster(container: str, *, docker: list[str]) -> None:
    docker_exec(
        container,
        "cd /home/verify/.meiva/source && .venv/bin/meiva init --serve-gateway --serve-agent-runner "
        "--machine-name clean --config-file /tmp/profile.env",
        docker=docker,
        timeout=300,
    )


def start_cluster(
    container: str, *services: str, docker: list[str]
) -> subprocess.CompletedProcess[str]:
    selection = " ".join(f"--only-service {service}" for service in services)
    return docker_exec_result(
        container,
        "cd /home/verify/.meiva/source && MEIVA_OS_JOBS_ENABLED=0 MEIVA_BROWSER_ENABLED=0 "
        f"MEIVA_TIMEZONE=UTC .venv/bin/meiva start {selection}",
        docker=docker,
        timeout=2400,
    )


def stop_cluster(container: str, *, docker: list[str]) -> None:
    docker_exec(
        container,
        "cd /home/verify/.meiva/source && MEIVA_OS_JOBS_ENABLED=0 .venv/bin/meiva stop --yes --close-shell-sessions --stop-browser --stop-extras --keep-no-services --stop-infra --mode smooth --timeout 240",
        docker=docker,
        timeout=300,
    )


def verify(container: str, *, docker: list[str]) -> None:
    docker_exec(
        container,
        "cd /home/verify/.meiva/source && uv sync --frozen --python 3.12",
        docker=docker,
        timeout=1800,
    )
    print("→ proving the packages are absent")
    absent = tool_presence(container, docker=docker)
    print(absent.strip())
    if "PRESENT:" in absent:
        raise CleanHostDepsError(f"the clean image already has host packages:\n{absent}")
    print("→ meiva init + first start with the frontend (installs every host package)")
    init_cluster(container, docker=docker)
    started = start_cluster(container, "gateway", "frontend", docker=docker)
    combined = started.stdout + started.stderr
    if "host dependencies" not in combined:
        raise CleanHostDepsError(
            "start did not run the host-dependency preflight:\n" + combined[-4000:]
        )
    if "host dependency preflight failed" in combined:
        raise CleanHostDepsError("the preflight refused on a clean host:\n" + combined[-4000:])
    print("→ proving the packages landed")
    present = tool_presence(container, docker=docker)
    print(present.strip())
    if "ABSENT:" in present:
        raise CleanHostDepsError(f"start returned but a package is still missing:\n{present}")
    if started.returncode != 0:
        print(
            "! the frontend roster did not reach ready on this ref (a build failure "
            "after the packages landed); the gateway-only start below still proves boot"
        )
    print("→ restarting with the gateway only (a clean, ready roster)")
    stop_cluster(container, docker=docker)
    gateway = start_cluster(container, "gateway", docker=docker)
    gateway_output = gateway.stdout + gateway.stderr
    if gateway.returncode != 0:
        raise CleanHostDepsError(f"gateway-only start failed:\n{gateway_output[-4000:]}")
    status = wait_until_serving(container, docker=docker)
    print(status[-2000:])


# meiva:note
# `meiva status` answers 503 on /api/cluster/status while start still holds the
# maintenance gate; the table above it is the readiness evidence. The gateway row
# carries its own probe result, and the infra rows name the data-plane processes
# this recipe is about.
def wait_until_serving(container: str, *, docker: list[str]) -> str:
    deadline = time.monotonic() + 120.0
    status = ""
    while time.monotonic() < deadline:
        status = docker_exec(
            container,
            "cd /home/verify/.meiva/source && .venv/bin/meiva status",
            docker=docker,
            timeout=300,
        )
        lines = status.splitlines()
        gateway = next((line for line in lines if line.startswith("meiva-gateway ")), "")
        healthy = all(
            any(line.strip().startswith(f"✓ {name} ") for line in lines)
            for name in ("postgres", "redis", "pgbouncer")
        )
        if "✓" in gateway and healthy:
            return status
        time.sleep(5)
    raise CleanHostDepsError(f"the gateway or its data plane is not serving:\n{status[-4000:]}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prove a clean host gets its missing packages from `meiva start`."
    )
    parser.add_argument("--ref", default="HEAD", help="commit to test (default HEAD)")
    parser.add_argument("--image", default="ubuntu:24.04")
    parser.add_argument("--keep", action="store_true", help="leave the container running")
    parser.add_argument("--docker", default="docker", help='docker command, e.g. "sudo -n docker"')
    args = parser.parse_args()

    if shutil.which("git") is None:
        print("git is not on PATH", file=sys.stderr)
        return 2
    docker = docker_argv(args.docker)
    if not docker_available(docker):
        print(
            f"docker is not runnable ({args.docker}); pass --docker 'sudo -n docker'",
            file=sys.stderr,
        )
        return 2

    name = f"meiva-clean-deps-{uuid.uuid4().hex[:8]}"
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "source.tar"
        profile = Path(tmp) / "profile.env"
        profile.write_text(PROFILE)
        print(f"→ archiving {args.ref}")
        git_archive(args.ref, source)
        print(f"→ starting {name} from {args.image}")
        run(
            [
                *docker,
                "run",
                "-d",
                "--init",
                "--name",
                name,
                "--shm-size=1g",
                args.image,
                "sleep",
                "infinity",
            ],
            timeout=120,
        )
        try:
            base_toolchain(name, docker=docker)
            copy_source(name, source, profile, docker=docker)
            verify(name, docker=docker)
            print("✓ clean host: start installed the missing packages and reached ready")
            return 0
        finally:
            if args.keep:
                print(f"→ kept container: {name}")
            else:
                run([*docker, "rm", "-f", name], timeout=120, check=False)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CleanHostDepsError, subprocess.TimeoutExpired) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
