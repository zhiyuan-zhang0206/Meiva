"""Host-side primitives shared by the verification recipes.

A verification boundary (a Linux container, a Tart macOS VM) is born from one commit,
runs one cluster through `meiva init` and `meiva start`, is observed, and is destroyed. What
every recipe shares lives here: the start profile (a scripted model, no key of any
kind), the init and start argv, the refusal to let host state in, the commit
resolution, and the evidence directory with its bounded, logged steps. The design is
future/infra/engineering/verification-boundaries.md.

Stdlib-only and host-side, like the recipes that import it.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import NotRequired, TypedDict

from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of

# The start profile (the deleted native preview's, plus a declared time zone): no cron,
# boot or logs jobs, no browser, no remote memory or transfer, no GitHub gate, no
# telemetry export, and the scripted model scenario in place of a provider. No key of
# any kind: nothing secret enters a boundary.
PROFILE = {
    "MEIVA_OS_JOBS_ENABLED": "0",
    "MEIVA_PROVISION_BUILTIN_SCHEDULES": "0",
    "MEIVA_BROWSER_ENABLED": "0",
    "MEIVA_MEMORY_KEEP_LOCAL": "1",
    "MEIVA_CROSS_MACHINE_TRANSFER_BACKEND": "none",
    "MEIVA_REQUIRE_GITHUB_PR": "0",
    "MEIVA_LLM_OVERRIDE": "tests.e2e.fakes.scenarios.message_flow:build",
    "MEIVA_TELEMETRY_OTLP_ENABLED": "0",
    "MEIVA_TIMEZONE": "UTC",
}
SERVICES = ("gateway", "frontend", "ops", "agent-host")
MACHINE_NAME = "verify"

# `meiva init` records the machine's identity; `meiva start` takes only the service selection.
START_ARGV = [
    ".venv/bin/meiva",
    "start",
    *(arg for name in SERVICES for arg in ("--only-service", name)),
]


def profile_text() -> str:
    return "".join(f"{key}={value}\n" for key, value in PROFILE.items())


def init_argv(profile_path: str) -> list[str]:
    return [
        ".venv/bin/meiva",
        "init",
        "--serve-gateway",
        "--serve-agent-runner",
        "--machine-name",
        MACHINE_NAME,
        "--machine-host",
        "127.0.0.1",
        "--config-file",
        profile_path,
    ]


class RunFailedError(RuntimeError):
    """A recipe step failed; the evidence of the steps before it is kept."""


# --------------------------------------------------------------------- the boundary


def refuse_host_state(source: Path, *, home: Path) -> None:
    """Nothing of the host's cluster, credentials, keychain or VM store may enter.

    Refuses the home directory, anything that contains it (a parent such as `/Users`
    would expose it), the host cluster, credential, keychain (`Library`) and Tart
    (`.tart`) directories inside it, and any socket (the container runtime's is one,
    and its path varies by runtime).
    """
    resolved_home = home.resolve()
    resolved = source.resolve()
    sealed = [
        resolved_home / name
        for name in (".meiva", ".ssh", ".gnupg", ".aws", ".docker", ".tart", "Library")
    ]
    if (
        resolved_home.is_relative_to(resolved)
        or any(resolved.is_relative_to(path) for path in sealed)
        or resolved.name == "docker.sock"
        or resolved.is_socket()
    ):
        raise ValueError(f"refusing to mount host state into the boundary: {resolved}")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 — argv list, never a shell
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def resolve_commit(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")


# ------------------------------------------------------------------------- evidence


class StepRow(TypedDict):
    # meiva:note
    # One recorded step: the command (None for an in-process action), its log, and how it ended.

    name: str
    argv: list[str] | None
    log: str | None
    returncode: NotRequired[int | str]
    error: NotRequired[str]
    seconds: NotRequired[float]


class ImageInfo(TypedDict):
    tag: str
    built: bool
    id: NotRequired[str]
    architecture: NotRequired[str]
    size_bytes: NotRequired[int]
    base: NotRequired[str]


class RunResult(TypedDict):
    # meiva:note
    # The document `result.json` holds: what the run was asked, every step, and the keys each recipe
    # adds as it goes (the container or VM it used, the evidence it copied out, what it removed).

    ref: str
    commit: str
    steps: list[StepRow]
    result: str
    recipe_repo: NotRequired[str]
    image: NotRequired[str | ImageInfo]
    vm: NotRequired[str]
    golden: NotRequired[str]
    tart: NotRequired[str]
    container: NotRequired[str]
    collected: NotRequired[dict[str, bool]]
    removed: NotRequired[bool]
    error: NotRequired[str]
    collect_error: NotRequired[str]
    cleanup_error: NotRequired[str]
    observer_error: NotRequired[str]


@dataclass
class Evidence:
    """One run's directory: a log per step, the observer's JSON, and result.json."""

    root: Path
    result: RunResult
    timeouts: dict[str, int]

    def step(self, name: str, argv: list[str], *, stdin: str | None = None) -> None:
        log = self.root / f"{len(self.result['steps']):02d}-{name}.log"
        row = StepRow(name=name, argv=argv, log=log.name)
        self.result["steps"].append(row)
        started = time.monotonic()
        try:
            with log.open("w") as out:
                done = subprocess.run(  # noqa: S603 — argv list, never a shell
                    argv,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    input=stdin,
                    stdin=None if stdin is not None else subprocess.DEVNULL,
                    text=True,
                    timeout=self.timeouts[name],
                    check=False,
                )
            row["returncode"] = done.returncode
        except subprocess.TimeoutExpired:
            row["returncode"] = "timeout"
        row["seconds"] = round(time.monotonic() - started, 1)
        self.save()
        if row["returncode"] != 0:
            raise RunFailedError(f"step {name} failed ({row['returncode']}): see {log}")

    @contextmanager
    def action(self, name: str) -> Generator[None]:
        """A step done in this process rather than by a command (booting a VM): timed and
        recorded like one, and an error in it fails the run."""
        row = StepRow(name=name, argv=None, log=None)
        self.result["steps"].append(row)
        started = time.monotonic()
        try:
            yield
            row["returncode"] = 0
        except BaseException as error:
            row["returncode"] = "error"
            row["error"] = repr(error)
            raise
        finally:
            row["seconds"] = round(time.monotonic() - started, 1)
            self.save()

    # meiva:note
    # Whether the observer's JSON says the run passed; a missing or malformed one fails the run
    # loudly: the reason goes to stderr and into `result.json` as `observer_error`.
    def observer_passed(self) -> bool:
        observer = self.root / "observer.json"
        problem: str | None = None
        passed = False
        try:
            document = object_of(json.loads, observer.read_text())
        except (OSError, json.JSONDecodeError) as error:
            problem = f"{observer} cannot be read: {error}"
        else:
            if not is_object_dict(document) or "result" not in document:
                problem = f"{observer} is not an object carrying a result"
            else:
                passed = document["result"] == "passed"
        if problem is not None:
            self.result["observer_error"] = problem
            print(f"observer: {problem}", file=sys.stderr, flush=True)
        return passed

    def save(self) -> None:
        (self.root / "result.json").write_text(json.dumps(self.result, indent=2) + "\n")
