from __future__ import annotations

import hashlib
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import base.paths
from base.config import settings
from base.host.permissions_helper.client import HelperFactory
from base.host.proc import run_bounded
from base.paths import logs_dir, meiva_home, permissions_helper_socket
from services.desktop.permissions_helper import activation_plan, hardened_runtime, signing_identity
from services.desktop.permissions_helper.build_state import (
    BUILD_STATE_NAME,
    BuildState,
    read_build_state,
    write_build_state,
)
from services.desktop.permissions_helper.launchd_job import (
    HELPER_BUNDLE_ID,
    BoundedRunner,
    RetirementEnv,
    clear_helper_stop_intent,
    helper_job_agents_dir,
    helper_job_domain,
    helper_job_label,
    helper_job_loaded,
    helper_job_plist_path,
    helper_stop_intent,
    require_retired_helper,
)

_SERVICE_DIR = Path(__file__).resolve().parent
SOURCE = _SERVICE_DIR / "helper" / "main.swift"
INFO_PLIST = _SERVICE_DIR / "helper" / "Info.plist"
# The AppleEvents entitlement: what lets tccd build an attribution chain for
# helper-spawned `osascript`/AE children; without it the request dies as
# -1712/-609 before any prompt can appear.
ENTITLEMENTS = _SERVICE_DIR / "helper" / "helper.entitlements"
LOCALES = _SERVICE_DIR / "helper" / "locales"
_DESIGNATED_REQUIREMENT_RE = re.compile(r"^(?:# )?designated => (.+)$", re.MULTILINE)

# meiva:note
# Every tool below is local and has no network leg, so a bound only catches a wedged tool. It turns that wait into a failed converge step, which the update path already knows how to abort and recover from. None of these is a performance budget -- each sits well above what the work costs.
#
# The bound is applied by `base.host.proc.run_bounded`, not `subprocess.run(timeout=)`, which kills only the process Python spawned and leaves its descendants running. Every git-driving module bounds git through the same helper.
#
# `security` is here for the keychain unlock, import and identity reads of the signing identity -- local IPC; `openssl` generates one 2048-bit key and one PKCS#12 archive, once per host.
TIMEOUTS_S = {
    "swiftc": 300.0,  # one small file; the ceiling only catches a wedged toolchain
    "codesign": 60.0,  # signs a few-MB bundle in well under a second
    "launchctl": 30.0,  # local IPC with launchd
    "security": 30.0,
    "openssl": 60.0,
}

# Stands in for the exit status of a call the bound killed. 124 is `timeout(1)`'s
# convention and none of the tools above uses it, so `run_readonly_query` can hand a hang back
# as a result instead of an exception without colliding with a real exit code.
_TIMED_OUT_RC = 124


class PermissionsHelperBuildError(RuntimeError):
    """A build, signing, or launchd step failed."""


# meiva:note
# A step outlived its bound and its process tree was killed.
class PermissionsHelperTimeoutError(PermissionsHelperBuildError): ...


@dataclass(frozen=True, slots=True)
class HelperSources:
    source: Path = SOURCE
    info_plist: Path = INFO_PLIST
    entitlements: Path = ENTITLEMENTS
    locales: Path = LOCALES


_DEFAULT_SOURCES = HelperSources()


@dataclass(frozen=True, slots=True)
class BuildEnv:
    app_dir: Path | None = None
    home: Path | None = None
    sources: HelperSources = _DEFAULT_SOURCES
    source_hash: str | None = None
    retirement: RetirementEnv | None = None
    runner: BoundedRunner | None = None


@dataclass(frozen=True, slots=True)
class InstallEnv:
    home: Path | None = None
    agents: Path | None = None
    label: str | None = None
    domain: str | None = None
    plist: Path | None = None
    loaded: bool | None = None
    log_dir: Path | None = None
    socket: Path | None = None
    artifact_dir: Path | None = None
    build: BuildEnv | None = None
    runner: BoundedRunner | None = None


# meiva:note
# Each tool is known by its name, whatever path it was invoked through: the
# signing identity names `/usr/bin/security` and `/usr/bin/openssl` absolutely so
# a Homebrew `openssl` cannot shadow them.
def _bound(cmd: list[str]) -> float:
    return TIMEOUTS_S[Path(cmd[0]).name]


# meiva:note
# Run a step that must succeed, raising `PermissionsHelperBuildError` if it does not -- including when it outlived its `TIMEOUTS_S` bound, which is a failure of the same kind and is reported as one so the converge step aborts rather than blocks.
def run_tool(
    cmd: list[str], *, runner: BoundedRunner | None = None
) -> subprocess.CompletedProcess[bytes]:
    timeout = _bound(cmd)
    run = run_bounded if runner is None else runner
    try:
        proc = run(cmd, timeout=timeout, capture_output=True)
    except subprocess.TimeoutExpired as exc:
        raise PermissionsHelperTimeoutError(
            f"{cmd[0]} did not finish within {timeout:.0f}s and its process tree was killed"
        ) from exc
    if proc.returncode != 0:
        raise PermissionsHelperBuildError(
            f"{cmd[0]} failed ({proc.returncode}): {proc.stderr.decode(errors='replace').strip()}"
        )
    return proc


# meiva:note
# Run a read-only query, folding a timeout into the returned result.
#
# Every caller here asks a yes/no question -- is this bundle validly signed, is that job loaded -- and already reads a non-zero exit as "no". A tool that hung is the same "no" with a different cause, so it comes back as `_TIMED_OUT_RC` with the reason on stderr instead of as an exception raised into paths whose whole point is not to raise.
def run_readonly_query(
    cmd: list[str], *, timeout: float | None = None, runner: BoundedRunner | None = None
) -> subprocess.CompletedProcess[bytes]:
    bound = _bound(cmd) if timeout is None else timeout
    run = run_bounded if runner is None else runner
    try:
        return run(cmd, timeout=bound, capture_output=True)
    except subprocess.TimeoutExpired:
        detail = f"`{' '.join(cmd[:2])}` timed out after {bound:.0f}s".encode()
        return subprocess.CompletedProcess(cmd, _TIMED_OUT_RC, b"", detail)


def source_content_hash(sources: HelperSources = _DEFAULT_SOURCES) -> str:
    """Hash every input that determines the built helper bundle."""
    digest = hashlib.sha256()
    digest.update(sources.source.read_bytes())
    digest.update(sources.info_plist.read_bytes())
    digest.update(sources.entitlements.read_bytes())
    for path in sorted(p for p in sources.locales.rglob("*") if p.is_file()):
        digest.update(path.relative_to(sources.locales).as_posix().encode() + path.read_bytes())
    # A signing-policy change is a new artifact, never an in-place re-sign.
    digest.update(" ".join(hardened_runtime.SIGNING_OPTIONS).encode())
    digest.update(signing_identity.policy().encode())
    return digest.hexdigest()


# meiva:note
# Return the app's designated requirement, which for ad-hoc code names its hash.
def read_dr(app: Path, *, runner: BoundedRunner | None = None) -> str:
    proc = run_tool(["codesign", "-d", "-r-", str(app)], runner=runner)
    # codesign writes the designated-requirement line to stdout on current
    # macOS (stderr carries `Executable=...`); older toolchains have emitted
    # it on stderr, so search both streams rather than pinning one.
    text = proc.stdout.decode(errors="replace") + proc.stderr.decode(errors="replace")
    match = _DESIGNATED_REQUIREMENT_RE.search(text)
    if match is None:
        raise PermissionsHelperBuildError(
            "codesign did not report a designated requirement for the permissions helper"
        )
    return match.group(1)


def app_executable(app: Path) -> Path:
    return app / "Contents" / "MacOS" / "MeivaPermissionsHelper"


def is_valid_app(app: Path, *, runner: BoundedRunner | None = None) -> bool:
    return (
        app_executable(app).exists()
        and run_readonly_query(["codesign", "--verify", str(app)], runner=runner).returncode == 0
    )


def remove_app(app: Path) -> None:
    if app.is_symlink() or app.is_file():
        app.unlink(missing_ok=True)
    else:
        shutil.rmtree(app, ignore_errors=True)


def _installed_build_is_current(
    app: Path,
    state: BuildState | None,
    source_hash: str,
    *,
    runner: BoundedRunner | None = None,
) -> bool:
    return (
        state is not None
        and state["source_hash"] == source_hash
        and is_valid_app(app, runner=runner)
    )


# meiva:note
# The build record of the installed helper, or None when this home has none: the one place
# that knows where the installed artifact keeps it.
def installed_build_state(app_dir: Path | None = None) -> BuildState | None:
    from base.paths import permissions_helper_app_dir

    root = permissions_helper_app_dir() if app_dir is None else app_dir
    return read_build_state(root / BUILD_STATE_NAME)


def build_directory(
    destination: Path | None,
    *,
    home: Path | None = None,
    app_dir: Path | None = None,
) -> Path:
    """Choose an immutable artifact destination outside the installed bundle tree."""
    from base.paths.home import default_home

    if app_dir is not None:
        installed = app_dir
    elif home is not None:
        installed = home / "helper"
    else:
        installed = base.paths.permissions_helper_app_dir()
    build_dir = installed if destination is None else destination
    protected = {installed.resolve(), (default_home() / "helper").resolve()}
    if destination is not None and (
        not destination.is_absolute()
        or destination.resolve() != destination
        or any(
            destination == path or path in destination.parents or destination in path.parents
            for path in protected
        )
    ):
        raise PermissionsHelperBuildError(
            "isolated helper destination must be canonical and outside the installed bundle directory"
        )
    return build_dir


# meiva:note
# Whether this rebuild replaces an existing stale artifact, refusing when it must not.
#
# An isolated destination's artifact is immutable. The installed one is replaced only once `meiva stop` retired this home's helper -- it is meiva-root's parent -- and that is proved before the compile, so a host still running it is turned away before anything is built or written.
def _replaces_installed(
    exe: Path,
    destination: Path | None,
    *,
    retirement: RetirementEnv | None = None,
) -> bool:
    if not exe.exists():
        return False
    if destination is not None:
        raise PermissionsHelperBuildError(
            "signed helper artifacts are immutable; prepare a new MEIVA_PERMISSIONS_HELPER_ARTIFACT_DIR "
            "and activate it after the home-specific helper job has been stopped and unregistered"
        )
    require_retired_helper(exe, env=retirement)
    return True


# meiva:note
# Compile and sign the helper with the fixed identity; return (app bundle path, rebuilt).
#
# Skips the compile + sign only when the installed bundle is valid and its recorded content hash matches every build input. Checkout mtimes therefore cannot churn the binary or its TCC identity. A stale installed bundle is rebuilt in place, only when `_replaces_installed` admits it. A rebuild is only a new code identity when the certificate changes: the designated requirement names the certificate, not the code hash, so the desktop grants survive it.
#
# The signature this build will carry is settled before anything is removed. A host whose recorded requirement names a certificate has nothing to fall back to, so a missing identity stops the build here rather than after the old artifact is gone.
# meiva:link services/docs/agent_runner_side/permissions-helper/authorization.meiva.okf.md
def build_and_sign(
    *, destination: Path | None = None, env: BuildEnv | None = None
) -> tuple[Path, bool]:
    cfg = env or BuildEnv()
    build_dir = build_directory(destination, home=cfg.home, app_dir=cfg.app_dir)
    state_path = build_dir / BUILD_STATE_NAME
    app = build_dir / "MeivaPermissionsHelper.app"
    exe = app_executable(app)
    source_hash = (
        cfg.source_hash if cfg.source_hash is not None else source_content_hash(cfg.sources)
    )
    state = read_build_state(state_path)

    if _installed_build_is_current(app, state, source_hash, runner=cfg.runner):
        return app, False
    _require_signing_identity(state, app_dir=cfg.app_dir, runner=cfg.runner)
    replacing = _replaces_installed(exe, destination, retirement=cfg.retirement)

    build_dir.mkdir(parents=True, exist_ok=True)
    binary = build_dir / "MeivaPermissionsHelper"
    run_tool(
        ["swiftc", "-O", str(cfg.sources.source), "-o", str(binary)],
        runner=cfg.runner,
    )

    if replacing:
        require_retired_helper(
            exe, env=cfg.retirement
        )  # proved again right before the old artifact goes
    remove_app(app)
    exe.parent.mkdir(parents=True, exist_ok=True)
    (app / "Contents" / "Info.plist").write_bytes(cfg.sources.info_plist.read_bytes())
    exe.write_bytes(binary.read_bytes())
    exe.chmod(0o755)
    for lproj in sorted(cfg.sources.locales.glob("*.lproj")):
        shutil.copytree(lproj, app / "Contents" / "Resources" / lproj.name, dirs_exist_ok=True)

    run_tool(
        signing_identity.sign_arguments(
            app,
            entitlements=cfg.sources.entitlements,
            identifier=HELPER_BUNDLE_ID,
            app_dir=cfg.app_dir,
        ),
        runner=cfg.runner,
    )
    actual_dr = read_dr(app, runner=cfg.runner)
    write_build_state(
        state_path,
        source_hash,
        actual_dr,
        signed_at=datetime.now(UTC).isoformat(),
    )
    if state is not None and state["dr"] != actual_dr:
        sys.stderr.write(
            "  ! permissions-helper: rebuilt with a new code identity — "
            "macOS permissions (Screen Recording, Accessibility, ...) need granting again\n"
        )
    return app, True


# meiva:note
# Whether this build may mint the signing identity, or must find it.
#
# No recorded requirement (first build on this host) or one that names a code
# hash (an ad-hoc artifact) means there is nothing to lose: provisioning the
# identity is the ordinary first step. A recorded certificate requirement means
# the installed helper already has an identity, and a missing keychain then is a
# deletion, not a first run -- signing anything else would cost every grant.
def _require_signing_identity(
    state: BuildState | None,
    *,
    app_dir: Path | None = None,
    runner: BoundedRunner | None = None,
) -> None:
    create = state is None or "certificate" not in state["dr"]
    try:
        signing_identity.ensure(
            lambda cmd: run_tool(cmd, runner=runner),
            create=create,
            app_dir=app_dir,
        )
    except signing_identity.SigningIdentityError as exc:
        raise PermissionsHelperBuildError(str(exc)) from exc


def label(home: Path | None = None) -> str:
    return helper_job_label(home)


def agents_dir(agents: Path | None = None) -> Path:
    return helper_job_agents_dir(agents)


def plist_path(home: Path | None = None, *, agents: Path | None = None) -> Path:
    return helper_job_plist_path(home, agents_dir=agents)


def launchd_domain() -> str:
    return helper_job_domain()


def is_loaded(home: Path | None = None, *, runner: BoundedRunner | None = None) -> bool:
    return helper_job_loaded(home, runner=runner)


def foreign_label_plists(
    *,
    agents: Path | None = None,
    socket: Path | None = None,
    current_label: str | None = None,
) -> list[Path]:
    """LaunchAgent plists bound to this cluster's socket under a foreign label.

    A job whose plist pins this cluster's socket but carries a label other than
    the current one races this cluster's own job for the same socket; which one
    a client reaches then depends on the last bind. Other
    clusters' jobs are left alone -- they pin their own sockets."""
    sock = str(helper_socket() if socket is None else socket)
    current = label() if current_label is None else current_label
    foreign: list[Path] = []
    for plist in agents_dir(agents).glob(f"{HELPER_BUNDLE_ID}.*.plist"):
        try:
            data = plistlib.loads(plist.read_bytes())
        except (plistlib.InvalidFileException, OSError):
            continue
        if activation_plan.is_foreign_label_job(data, label=current, socket=sock):
            foreign.append(plist)
    return foreign


def refuse_foreign_label_jobs(
    *,
    agents: Path | None = None,
    socket: Path | None = None,
    current_label: str | None = None,
) -> None:
    foreign = foreign_label_plists(agents=agents, socket=socket, current_label=current_label)
    if foreign:
        raise PermissionsHelperBuildError(
            "foreign helper labels bind this home's socket; reconcile them externally before start: "
            + ", ".join(str(path) for path in foreign)
        )


def install_and_load(
    app: Path,
    *,
    rebuilt: bool,
    helper: HelperFactory,
    settle_seconds: float = activation_plan.HELPER_PING_SETTLE_S,
    env: InstallEnv | None = None,
) -> None:
    """Write the LaunchAgent plist and ensure launchd is running this binary.

    A loaded helper is only observed. Replacement requires an external stop
    and exact-home unregister first, so a descendant cannot unload its parent."""
    cfg = env or InstallEnv()
    home = base.paths.meiva_home() if cfg.home is None else cfg.home
    job_label = label(cfg.home) if cfg.label is None else cfg.label
    sock = helper_socket(cfg.home) if cfg.socket is None else cfg.socket
    refuse_foreign_label_jobs(agents=cfg.agents, socket=sock, current_label=job_label)
    log = (logs_dir() if cfg.log_dir is None else cfg.log_dir) / "permissions-helper.log"
    plist = activation_plan.helper_plist(
        label=job_label,
        executable=str(app_executable(app)),
        log=str(log),
        socket=str(sock),
        root_seed=str(base.paths.root_run_dir(home) / "seed.json"),
    )
    path = plist_path(cfg.home, agents=cfg.agents) if cfg.plist is None else cfg.plist
    path.parent.mkdir(parents=True, exist_ok=True)
    new_bytes = plistlib.dumps(plist)
    loaded = is_loaded(cfg.home, runner=cfg.runner) if cfg.loaded is None else cfg.loaded
    plan = activation_plan.activation_plan(
        loaded=loaded,
        stop_intent=loaded and helper_stop_intent(home),
        plist_changed=not path.exists() or path.read_bytes() != new_bytes,
        rebuilt=rebuilt,
    )
    if plan.refusal is not None:
        raise PermissionsHelperBuildError(plan.refusal)
    if plan.bootstrap:
        base.paths.root_run_dir(home).mkdir(mode=0o700, parents=True, exist_ok=True)
        clear_helper_stop_intent(home)
        path.write_bytes(new_bytes)
        domain = launchd_domain() if cfg.domain is None else cfg.domain
        run_tool(["launchctl", "bootstrap", domain, str(path)], runner=cfg.runner)
    if not helper_answers_ping(helper, settle_seconds=settle_seconds):
        raise PermissionsHelperBuildError(
            "permissions helper is unresponsive; custody is unknown. Diagnose the native job "
            "externally; start will not bootout or kickstart its possible ancestor"
        )


def helper_socket(home: Path | None = None) -> Path:
    """This cluster's permissions-helper socket, from its configured helper port."""
    return permissions_helper_socket(
        meiva_home() if home is None else home,
        settings.services.permissions_helper_port,
    )


# meiva:note
# Ping the helper until it answers, up to `attempts` times with `settle_seconds` between them: a
# freshly bootstrapped helper needs a moment before its socket answers. A helper that answers
# without the durable stop/shutdown protocol is an error, not a retry.
def helper_answers_ping(
    helper: HelperFactory,
    *,
    attempts: int = activation_plan.HELPER_PING_ATTEMPTS,
    settle_seconds: float = activation_plan.HELPER_PING_SETTLE_S,
) -> bool:
    from base.host.permissions_helper import client

    for attempt in range(attempts):
        try:
            reply = helper().ping()
        except (client.PermissionsHelperError, OSError, ValueError):
            reply = None
        verdict = activation_plan.ping_verdict(reply)
        if verdict == "needs_upgrade":
            raise PermissionsHelperBuildError(
                "helper lacks the durable stop/shutdown protocol; upgrade its signed artifact "
                "after external exact-home retirement before starting"
            )
        if verdict == "answered":
            return True
        if attempt < attempts - 1:
            time.sleep(settle_seconds)
    return False


def _require_usable_socket_paths(*, home: Path | None = None, socket: Path | None = None) -> None:
    """Refuse impossible macOS IPC names before signing or registering jobs."""
    cluster_home = base.paths.meiva_home() if home is None else home
    paths = (
        helper_socket(home) if socket is None else socket,
        base.paths.root_run_dir(cluster_home) / "meiva-root.sock",
    )
    for path in paths:
        if activation_plan.socket_path_too_long(os.fsencode(path)):
            raise PermissionsHelperBuildError(
                f"macOS IPC path exceeds 103 bytes: {path}; choose a shorter cluster home"
            )


# meiva:note
# Idempotent full bring-up: build+sign, load. Raises on any failure.
def converge(*, helper: HelperFactory, env: InstallEnv | None = None) -> None:
    cfg = env or InstallEnv()
    _require_usable_socket_paths(home=cfg.home, socket=cfg.socket)
    dest = (
        settings.services.permissions_helper_artifact_dir
        if cfg.artifact_dir is None
        else cfg.artifact_dir
    )
    app, rebuilt = build_and_sign(destination=dest, env=cfg.build)
    install_and_load(app, rebuilt=rebuilt, helper=helper, env=cfg)
