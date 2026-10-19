#!/usr/bin/env python3
# meiva:note
# The clean-host refusal recipe: boot ubuntu:24.04 with no cron and no systemd,
# run `meiva start` with OS jobs enabled, and prove it refuses in the host
# dependency preflight — naming the tool and its repair — before converge reaches
# any later step and before a single service starts. Then hand sudo back and
# prove the second start installs `cron` through scripts/provision/host_utils.sh
# and refuses only on the host-owned systemd. It is a one-off verification
# recipe, not a CI gate: apt and PyPI need the network and the run takes minutes.
# meiva:link cli/commands/lifecycle/docs/start-readiness.meiva.okf.md

from __future__ import annotations

import argparse
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROFILE = """\
MEIVA_PROVISION_BUILTIN_SCHEDULES=0
MEIVA_BROWSER_ENABLED=0
MEIVA_MEMORY_KEEP_LOCAL=1
MEIVA_CROSS_MACHINE_TRANSFER_BACKEND=none
MEIVA_REQUIRE_GITHUB_PR=0
MEIVA_TELEMETRY_OTLP_ENABLED=0
MEIVA_TIMEZONE=UTC
"""
_SERVICES = ("gateway",)


def start_argv(services: tuple[str, ...]) -> str:
    return ".venv/bin/meiva start " + " ".join(f"--only-service {name}" for name in services)


class RefusalError(RuntimeError):
    pass


def docker_argv(command: str = "docker") -> list[str]:
    return shlex.split(command)


def docker_available(docker: list[str]) -> bool:
    return bool(docker) and (shutil.which(docker[0]) is not None or Path(docker[0]).exists())


# meiva:note
# argv is assembled from operator-supplied executable names and fixed arguments;
# no shell is involved.
def run(
    argv: list[str], *, timeout: float = 900.0, check: bool = True
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(  # noqa: S603
        argv, capture_output=True, text=True, timeout=timeout, check=False
    )
    if check and result.returncode != 0:
        raise RefusalError(
            f"command failed ({result.returncode}): {shlex.join(argv)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def sh(
    container: str, script: str, *, docker: list[str], user: str = "verify", timeout: float = 900.0
) -> subprocess.CompletedProcess[str]:
    return run(
        [*docker, "exec", "-u", user, container, "bash", "-lc", script],
        timeout=timeout,
        check=False,
    )


def sh_ok(
    container: str,
    script: str,
    *,
    docker: list[str],
    user: str = "verify",
    timeout: float = 900.0,
) -> str:
    result = sh(container, script, docker=docker, user=user, timeout=timeout)
    if result.returncode != 0:
        raise RefusalError(
            f"container step failed ({result.returncode}): {script}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout


# meiva:note
# git archive is a fixed argv with the operator-supplied ref; the tar stream goes
# straight to a file so the binary payload is never captured as text.
def git_archive(ref: str, target: Path) -> None:
    with target.open("wb") as handle:
        result = subprocess.run(  # noqa: S603
            ["git", "-C", str(REPO), "archive", "--format=tar", ref],
            stdout=handle,
            stderr=subprocess.PIPE,
            check=False,
        )
    if result.returncode != 0:
        raise RefusalError(f"git archive {ref} failed: {result.stderr.decode(errors='replace')}")


def base_toolchain(container: str, *, docker: list[str]) -> None:
    sh_ok(
        container,
        "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
        "--no-install-recommends python3.12 python3.12-venv python3.12-dev "
        "build-essential ca-certificates git sudo locales >/dev/null "
        "&& locale-gen en_US.UTF-8 >/dev/null "
        "&& useradd --create-home --shell /bin/bash verify "
        "&& echo 'verify ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/verify "
        "&& chmod 440 /etc/sudoers.d/verify",
        docker=docker,
        user="root",
    )
    # meiva:note
    # `cron` is deliberately NOT installed: this recipe proves the missing-crontab
    # refusal on a host whose only scheduler is absent, like a slim container image.
    presence = sh_ok(
        container,
        "command -v crontab >/dev/null && echo CRON-PRESENT || echo cron-absent; "
        "[ -d /run/systemd/system ] && echo systemd-running || echo systemd-absent",
        docker=docker,
    )
    if "CRON-PRESENT" in presence:
        raise RefusalError(
            "the base image already carries crontab; the recipe cannot prove a refusal"
        )


def copy_source(container: str, source: Path, profile: Path, *, docker: list[str]) -> None:
    uv = shutil.which("uv")
    if uv is None:
        raise RefusalError("uv is not on PATH — the container needs the host's uv binary")
    run([*docker, "cp", str(Path(uv).resolve()), f"{container}:/usr/local/bin/uv"], timeout=60)
    sh_ok(container, "chmod +x /usr/local/bin/uv", docker=docker, user="root")
    sh_ok(container, "mkdir -p /home/verify/.meiva/source", docker=docker)
    run([*docker, "cp", str(source), f"{container}:/tmp/source.tar"], timeout=120)
    run([*docker, "cp", str(profile), f"{container}:/tmp/profile.env"], timeout=60)
    sh_ok(
        container,
        "tar -xf /tmp/source.tar -C /home/verify/.meiva/source "
        "&& chown -R verify:verify /home/verify/.meiva",
        docker=docker,
        user="root",
    )
    # meiva:note
    # The migrator only applies files git tracks and refuses a bare directory; a
    # synthetic commit gives the archive the worktree shape a real clone has.
    sh_ok(
        container,
        "cd /home/verify/.meiva/source && git init -q && git add -A "
        "&& git -c user.email=verify@example.invalid -c user.name=verify "
        "commit -qm 'clean-host archive' && git rev-parse HEAD",
        docker=docker,
    )


def prepare(container: str, *, docker: list[str]) -> None:
    sh_ok(
        container,
        "cd /home/verify/.meiva/source && uv sync --frozen --python 3.12",
        docker=docker,
        timeout=1800,
    )
    sh_ok(
        container,
        "cd /home/verify/.meiva/source && .venv/bin/meiva init --serve-gateway "
        "--serve-agent-runner --machine-name clean --config-file /tmp/profile.env",
        docker=docker,
        timeout=300,
    )


def start(
    container: str,
    *,
    os_jobs: bool,
    docker: list[str],
    services: tuple[str, ...] = _SERVICES,
) -> subprocess.CompletedProcess[str]:
    env = "MEIVA_BROWSER_ENABLED=0 MEIVA_TIMEZONE=UTC "
    if not os_jobs:
        env += "MEIVA_OS_JOBS_ENABLED=0 "
    return sh(
        container,
        f"cd /home/verify/.meiva/source && {env}{start_argv(services)}",
        docker=docker,
        timeout=2400,
    )


def running_services(container: str, *, docker: list[str]) -> str:
    # meiva:note
    # The bracket keeps each pattern from matching the grep/pgrep's own command
    # line, which would otherwise report a "started service" that never existed.
    return sh_ok(
        container,
        "pgrep -f '[m]eiva-gateway' || pgrep -x postgres || pgrep -x redis-server "
        "|| pgrep -x pgbouncer || true",
        docker=docker,
    )


def phase_refusal(container: str, *, docker: list[str]) -> None:
    print("→ phase 1: no cron, no sudo, OS jobs on — start must refuse before converge")
    # meiva:note
    # Dropping sudo is what makes the provisioners fail fast instead of installing
    # for minutes first; the refusal itself does not depend on it (a host-owned
    # tool refuses before any installer runs).
    sh_ok(container, "sudo -n rm -f /etc/sudoers.d/verify", docker=docker)
    result = start(container, os_jobs=True, docker=docker)
    out = result.stdout + result.stderr
    print(out[-3000:])
    if result.returncode == 0:
        raise RefusalError("start succeeded on a host without crontab or systemd")
    for expected in (
        "host dependency preflight failed",
        "✗ crontab",
        "✗ systemctl",
        "scripts/provision/host_utils.sh",
        "MEIVA_OS_JOBS_ENABLED=0",
    ):
        if expected not in out:
            raise RefusalError(f"the refusal did not name {expected!r}:\n{out[-4000:]}")
    if "PostgreSQL 17 + pgvector runtime" in out:
        raise RefusalError("the refusal came after converge had already run later steps")
    started = running_services(container, docker=docker).strip()
    if started:
        raise RefusalError(f"a service started despite the refusal:\n{started}")


def phase_install(container: str, *, docker: list[str]) -> None:
    print("→ phase 2: sudo back — start installs cron through the provisioner")
    sh_ok(
        container,
        "printf 'verify ALL=(ALL) NOPASSWD:ALL\\n' > /etc/sudoers.d/verify "
        "&& chmod 440 /etc/sudoers.d/verify",
        docker=docker,
        user="root",
    )
    first = start(container, os_jobs=True, docker=docker)
    out_first = first.stdout + first.stderr
    print(out_first[-3000:])
    if first.returncode == 0:
        raise RefusalError("the start succeeded with no systemd to install the boot unit")
    installed = sh_ok(container, "command -v crontab || true", docker=docker).strip()
    if not installed:
        raise RefusalError(f"the preflight did not install cron:\n{out_first[-4000:]}")
    if "✗ crontab" in out_first:
        raise RefusalError("crontab was reported missing after its install landed")
    if "✗ systemctl" not in out_first:
        raise RefusalError("the host-owned systemd refusal did not fire:\n" + out_first[-4000:])
    if "installing" not in out_first:
        raise RefusalError("the first start did not run the provisioner:\n" + out_first[-4000:])
    print("→ phase 2b: a second start must not reinstall")
    second = start(container, os_jobs=True, docker=docker)
    out_second = second.stdout + second.stderr
    if "installing" in out_second:
        raise RefusalError("the second start installed again:\n" + out_second[-4000:])
    if "✗ crontab" in out_second:
        raise RefusalError("the second start lost the installed crontab")


def phase_ready(container: str, *, docker: list[str]) -> None:
    print("→ phase 3: OS jobs off — the gateway must reach a ready boot")
    for attempt in (1, 2):
        result = start(container, os_jobs=False, docker=docker)
        out = result.stdout + result.stderr
        if result.returncode != 0:
            raise RefusalError(f"gateway-only start #{attempt} failed:\n{out[-4000:]}")
        if "installing" in out:
            raise RefusalError(
                f"start #{attempt} reinstalled a ready host's packages:\n{out[-4000:]}"
            )
    status = sh_ok(
        container, "cd /home/verify/.meiva/source && .venv/bin/meiva status", docker=docker
    )
    print(status[-2000:])
    if "✓" not in status:
        raise RefusalError("status shows no ready service")


def phase_node(container: str, *, docker: list[str]) -> None:
    print("→ phase 4: the frontend roster installs Node once and never again")
    # meiva:note
    # The running root owns an immutable generation; a different service selection
    # needs a stop first (the same rule an operator meets by hand).
    sh_ok(
        container,
        "cd /home/verify/.meiva/source && MEIVA_OS_JOBS_ENABLED=0 .venv/bin/meiva stop -y",
        docker=docker,
        timeout=900,
    )
    first = start(container, os_jobs=False, docker=docker, services=("gateway", "frontend"))
    out = first.stdout + first.stderr
    print(out[-3000:])
    if "installing Node.js" not in out:
        raise RefusalError(f"the frontend roster did not install Node:\n{out[-4000:]}")
    if "host dependency preflight failed" in out or "✗ host dependencies" in out:
        raise RefusalError(f"the preflight refused the frontend roster:\n{out[-4000:]}")
    if not sh_ok(container, "command -v node || true", docker=docker).strip():
        raise RefusalError("node is still missing after the install")
    second = start(container, os_jobs=False, docker=docker, services=("gateway", "frontend"))
    out_second = second.stdout + second.stderr
    if "installing" in out_second:
        raise RefusalError(f"the second frontend start reinstalled Node:\n{out_second[-4000:]}")
    if "✗ node" in out_second:
        raise RefusalError(f"the second frontend start misreported Node:\n{out_second[-4000:]}")
    print("  (a frontend build failure after the packages landed is pre-existing and tolerated)")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prove a host without crontab/systemd refuses before converge, then installs cron."
    )
    parser.add_argument("--ref", default="HEAD", help="commit to test (default HEAD)")
    parser.add_argument("--image", default="ubuntu:24.04")
    parser.add_argument("--keep", action="store_true", help="leave the container running")
    parser.add_argument("--docker", default="docker", help='docker command, e.g. "sudo -n docker"')
    args = parser.parse_args()
    docker = docker_argv(args.docker)
    if shutil.which("git") is None:
        print("git is not on PATH", file=sys.stderr)
        return 2
    if not docker_available(docker):
        print(
            f"docker is not runnable ({args.docker}); pass --docker 'sudo -n docker'",
            file=sys.stderr,
        )
        return 2
    name = f"meiva-dep-refusal-{uuid.uuid4().hex[:8]}"
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
            prepare(name, docker=docker)
            phase_refusal(name, docker=docker)
            phase_install(name, docker=docker)
            phase_ready(name, docker=docker)
            phase_node(name, docker=docker)
            print("✓ clean host: refused early, installed cron, then reached ready")
            return 0
        finally:
            if args.keep:
                print(f"→ kept container: {name}")
            else:
                run([*docker, "rm", "-f", name], timeout=120, check=False)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RefusalError, subprocess.TimeoutExpired) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
