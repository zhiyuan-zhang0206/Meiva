from __future__ import annotations

import hashlib
import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

import base.paths
from base.clock import Timers
from base.config import settings
from base.host.permissions_helper.client import HelperFactory
from base.host.proc import run_bounded
from base.paths import logs_dir, meiva_home, permissions_helper_socket
from services.desktop.permissions_helper import hardened_runtime, signing_identity
from services.desktop.permissions_helper.build_state import (
    BUILD_STATE_NAME,
    BuildState,
    read_build_state,
    write_build_state,
)
from services.desktop.permissions_helper.launchd_job import (
    HELPER_BUNDLE_ID,
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
_HELPER_PING_ATTEMPTS = 10
_HELPER_PING_SETTLE_S = 0.5
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


# meiva:note
# Each tool is known by its name, whatever path it was invoked through: the
# signing identity names `/usr/bin/security` and `/usr/bin/openssl` absolutely so
# a Homebrew `openssl` cannot shadow them.
def _bound(cmd: list[str]) -> float:
    return TIMEOUTS_S[Path(cmd[0]).name]


# meiva:note
# Run a step that must succeed, raising `PermissionsHelperBuildError` if it does not -- including when it outlived its `TIMEOUTS_S` bound, which is a failure of the same kind and is reported as one so the converge step aborts rather than blocks.
def run_tool(cmd: list[str]) -> subprocess.CompletedProcess[bytes]:
    timeout = _bound(cmd)
    try:
        proc = run_bounded(cmd, timeout=timeout, capture_output=True)
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
    cmd: list[str], *, timeout: float | None = None
) -> subprocess.CompletedProcess[bytes]:
    bound = _bound(cmd) if timeout is None else timeout
    try:
        return run_bounded(cmd, timeout=bound, capture_output=True)
    except subprocess.TimeoutExpired:
        detail = f"`{' '.join(cmd[:2])}` timed out after {bound:.0f}s".encode()
        return subprocess.CompletedProcess(cmd, _TIMED_OUT_RC, b"", detail)


def source_content_hash() -> str:
    """Hash every input that determines the built helper bundle."""
    digest = hashlib.sha256()
    digest.update(SOURCE.read_bytes())
    digest.update(INFO_PLIST.read_bytes())
    digest.update(ENTITLEMENTS.read_bytes())
    for path in sorted(p for p in LOCALES.rglob("*") if p.is_file()):
        digest.update(path.relative_to(LOCALES).as_posix().encode() + path.read_bytes())
    # A signing-policy change is a new artifact, never an in-place re-sign.
    digest.update(" ".join(hardened_runtime.SIGNING_OPTIONS).encode())
    digest.update(signing_identity.policy().encode())
    return digest.hexdigest()


# meiva:note
# Return the app's designated requirement, which for ad-hoc code names its hash.
def read_dr(app: Path) -> str:
    proc = run_tool(["codesign", "-d", "-r-", str(app)])
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


def is_valid_app(app: Path) -> bool:
    return (
        app_executable(app).exists()
        and run_readonly_query(["codesign", "--verify", str(app)]).returncode == 0
    )


def remove_app(app: Path) -> None:
    if app.is_symlink() or app.is_file():
        app.unlink(missing_ok=True)
    else:
        shutil.rmtree(app, ignore_errors=True)


def _installed_build_is_current(app: Path, state: BuildState | None, source_hash: str) -> bool:
    return state is not None and state["source_hash"] == source_hash and is_valid_app(app)


def build_directory(destination: Path | None) -> Path:
    """Choose an immutable artifact destination outside the installed bundle tree."""
    from base.paths.home import default_home

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
def _replaces_installed(exe: Path, destination: Path | None) -> bool:
    if not exe.exists():
        return False
    if destination is not None:
        raise PermissionsHelperBuildError(
            "signed helper artifacts are immutable; prepare a new MEIVA_PERMISSIONS_HELPER_ARTIFACT_DIR "
            "and activate it after the home-specific helper job has been stopped and unregistered"
        )
    require_retired_helper(exe)
    return True


# meiva:note
# Compile and sign the helper with the fixed identity; return (app bundle path, rebuilt).
#
# Skips the compile + sign only when the installed bundle is valid and its recorded content hash matches every build input. Checkout mtimes therefore cannot churn the binary or its TCC identity. A stale installed bundle is rebuilt in place, only when `_replaces_installed` admits it. A rebuild is only a new code identity when the certificate changes: the designated requirement names the certificate, not the code hash, so the desktop grants survive it.
#
# The signature this build will carry is settled before anything is removed. A host whose recorded requirement names a certificate has nothing to fall back to, so a missing identity stops the build here rather than after the old artifact is gone.
# meiva:link services/docs/agent_runner_side/permissions-helper/authorization.meiva.okf.md
def build_and_sign(*, destination: Path | None = None) -> tuple[Path, bool]:
    build_dir = build_directory(destination)
    state_path = build_dir / BUILD_STATE_NAME
    app = build_dir / "MeivaPermissionsHelper.app"
    exe = app_executable(app)
    source_hash = source_content_hash()
    state = read_build_state(state_path)

    if _installed_build_is_current(app, state, source_hash):
        return app, False
    _require_signing_identity(state)
    replacing = _replaces_installed(exe, destination)

    build_dir.mkdir(parents=True, exist_ok=True)
    binary = build_dir / "MeivaPermissionsHelper"
    run_tool(["swiftc", "-O", str(SOURCE), "-o", str(binary)])

    if replacing:
        require_retired_helper(exe)  # proved again right before the old artifact goes
    remove_app(app)
    exe.parent.mkdir(parents=True, exist_ok=True)
    (app / "Contents" / "Info.plist").write_bytes(INFO_PLIST.read_bytes())
    exe.write_bytes(binary.read_bytes())
    exe.chmod(0o755)
    for lproj in sorted(LOCALES.glob("*.lproj")):
        shutil.copytree(lproj, app / "Contents" / "Resources" / lproj.name, dirs_exist_ok=True)

    run_tool(
        signing_identity.sign_arguments(
            app, entitlements=ENTITLEMENTS, identifier=HELPER_BUNDLE_ID
        )
    )
    actual_dr = read_dr(app)
    write_build_state(state_path, source_hash, actual_dr)
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
def _require_signing_identity(state: BuildState | None) -> None:
    create = state is None or "certificate" not in state["dr"]
    try:
        signing_identity.ensure(run_tool, create=create)
    except signing_identity.SigningIdentityError as exc:
        raise PermissionsHelperBuildError(str(exc)) from exc


def label() -> str:
    return helper_job_label()


def agents_dir() -> Path:
    return helper_job_agents_dir()


def plist_path() -> Path:
    return helper_job_plist_path()


def launchd_domain() -> str:
    return helper_job_domain()


def is_loaded() -> bool:
    return helper_job_loaded()


def foreign_label_plists() -> list[Path]:
    """LaunchAgent plists bound to this cluster's socket under a foreign label.

    A job whose plist pins this cluster's socket but carries a label other than
    the current one races this cluster's own job for the same socket; which one
    a client reaches then depends on the last bind. Other
    clusters' jobs are left alone -- they pin their own sockets."""
    sock = str(helper_socket())
    foreign: list[Path] = []
    for plist in agents_dir().glob(f"{HELPER_BUNDLE_ID}.*.plist"):
        try:
            data = plistlib.loads(plist.read_bytes())
        except (plistlib.InvalidFileException, OSError):
            continue
        if data.get("Label") == label():
            continue
        env: dict[str, object] = data.get("EnvironmentVariables") or {}
        if env.get("MEIVA_PERMISSIONS_HELPER_SOCKET") == sock:
            foreign.append(plist)
    return foreign


def refuse_foreign_label_jobs() -> None:
    foreign = foreign_label_plists()
    if foreign:
        raise PermissionsHelperBuildError(
            "foreign helper labels bind this home's socket; reconcile them externally before start: "
            + ", ".join(str(path) for path in foreign)
        )


def install_and_load(app: Path, *, rebuilt: bool, helper: HelperFactory, timers: Timers) -> None:
    """Write the LaunchAgent plist and ensure launchd is running this binary.

    A loaded helper is only observed. Replacement requires an external stop
    and exact-home unregister first, so a descendant cannot unload its parent."""
    refuse_foreign_label_jobs()
    exe = app / "Contents" / "MacOS" / "MeivaPermissionsHelper"
    log = logs_dir() / "permissions-helper.log"
    plist = {
        "Label": label(),
        "ProgramArguments": [str(exe)],
        "EnvironmentVariables": {
            "MEIVA_PERMISSIONS_HELPER_SOCKET": str(helper_socket()),
            "MEIVA_PERMISSIONS_HELPER_ROOT_SEED": str(
                base.paths.root_run_dir(base.paths.meiva_home()) / "seed.json"
            ),
        },
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    new_bytes = plistlib.dumps(plist)
    plist_changed = not path.exists() or path.read_bytes() != new_bytes
    loaded = is_loaded()
    if loaded and helper_stop_intent(base.paths.meiva_home()):
        raise PermissionsHelperBuildError(
            "helper retirement is incomplete; finish meiva stop externally before starting"
        )
    if loaded and (plist_changed or rebuilt):
        raise PermissionsHelperBuildError(
            "loaded helper differs from the requested artifact or job; stop and unregister "
            "this home's helper from outside its process tree before activating the replacement"
        )
    if not loaded:
        base.paths.root_run_dir(base.paths.meiva_home()).mkdir(
            mode=0o700, parents=True, exist_ok=True
        )
        clear_helper_stop_intent(base.paths.meiva_home())
        path.write_bytes(new_bytes)
        run_tool(["launchctl", "bootstrap", launchd_domain(), str(path)])
    if not helper_answers_ping(helper, timers):
        raise PermissionsHelperBuildError(
            "permissions helper is unresponsive; custody is unknown. Diagnose the native job "
            "externally; start will not bootout or kickstart its possible ancestor"
        )


def helper_socket() -> Path:
    """This cluster's permissions-helper socket, from its configured helper port."""
    return permissions_helper_socket(meiva_home(), settings.services.permissions_helper_port)


def helper_answers_ping(helper: HelperFactory, timers: Timers) -> bool:
    from base.host.permissions_helper import client

    for attempt in range(_HELPER_PING_ATTEMPTS):
        try:
            reply = helper().ping()
        except (client.PermissionsHelperError, OSError, ValueError):
            reply = None
        if reply is not None and reply.get("pong") is True:
            if (
                reply.get("root_stop_intent_v1") is not True
                or reply.get("helper_shutdown_v1") is not True
            ):
                raise PermissionsHelperBuildError(
                    "helper lacks the durable stop/shutdown protocol; upgrade its signed artifact "
                    "after external exact-home retirement before starting"
                )
            return True
        if attempt < _HELPER_PING_ATTEMPTS - 1:
            timers.sleep(_HELPER_PING_SETTLE_S)
    return False


def _require_usable_socket_paths() -> None:
    """Refuse impossible macOS IPC names before signing or registering jobs."""
    paths = (helper_socket(), base.paths.root_run_dir(base.paths.meiva_home()) / "meiva-root.sock")
    for path in paths:
        # Darwin sockaddr_un.sun_path has 104 bytes, including the terminating NUL.
        if len(os.fsencode(path)) >= 104:
            raise PermissionsHelperBuildError(
                f"macOS IPC path exceeds 103 bytes: {path}; choose a shorter cluster home"
            )


# meiva:note
# Idempotent full bring-up: build+sign, load. Raises on any failure.
def converge(*, helper: HelperFactory, timers: Timers) -> None:
    _require_usable_socket_paths()
    app, rebuilt = build_and_sign(destination=settings.services.permissions_helper_artifact_dir)
    install_and_load(app, rebuilt=rebuilt, helper=helper, timers=timers)
