#!/usr/bin/env python3

from __future__ import annotations

import argparse
import os
import random
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from services.desktop.permissions_helper.lifecycle import BuildEnv

REPO_ROOT = Path(__file__).resolve().parents[3]
_CERT_ROOT = re.compile(r'certificate root = H"([0-9a-f]{40})"')
DESCRIPTION = (
    "The permissions helper's signing identity: inspect it, provision it, switch the install."
)


# meiva:note
# Inspect, provision, move and switch the permissions helper's signing identity.
#
# Usage from a checkout with its own `.venv`:
#     .venv/bin/python scripts/host_ops/tcc/helper-signing.py status
#     .venv/bin/python scripts/host_ops/tcc/helper-signing.py switch
#
# `switch` is the whole install-location change: back the installed helper up,
# rebuild it signed with the fixed identity, prove the designated requirement now
# names the certificate, and restore the backup if any step fails.
#
# A loaded helper is never replaced in place, so the operator stops this home
# first (`meiva stop --yes`, which also takes meiva-root and its agents down); the
# build refuses while the job is still loaded, by design.
#
# A rehearsal names its own home by exporting `MEIVA_HOME` before the command --
# the home is read when the app modules import, so this script never rewrites it.
#
# Re-run this command in its own session and return at once.
#
# `switch --retire` stops the helper, which is the parent of this home's
# meiva-root: a process inside that tree would be killed halfway through the
# swap. A new session survives the teardown, and its log is the only evidence
# left on the host -- `$MEIVA_HOME/logs`, where a recovery session looks -- so the
# path is printed before anything else happens.
def _detach(argv: list[str], cluster_home: Path) -> int:
    from base.paths import logs_dir

    log = (
        logs_dir(home=cluster_home)
        / f"helper-signing-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.log"
    )
    pid = os.fork()
    if pid:
        print(f"detached as pid {pid}; log: {log}")
        return 0
    os.setsid()
    descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.dup2(descriptor, 1)
    os.dup2(descriptor, 2)
    os.dup2(os.open(os.devnull, os.O_RDONLY), 0)
    args = [argument for argument in argv if argument != "--detach"]
    print(f"=== detached switch started {datetime.now(UTC).isoformat()} ===")
    try:
        return main(args)
    except BaseException as error:
        print(f"switch aborted: {error!r}")
        raise


# meiva:note
# Read `codesign`'s answer for one bundle: fixed argv, macOS tool.
def _codesign(*args: str, app: Path) -> str:
    result = subprocess.run(  # noqa: S603
        ["/usr/bin/codesign", *args, str(app)], capture_output=True, text=True, check=False
    )
    return (result.stdout or "") + (result.stderr or "")


# meiva:note
# Report the identity, the installed helper and the requirement it carries; reads
# `security find-identity` for this home's keychain only.
def status(env: BuildEnv) -> int:
    from services.desktop.permissions_helper import signing_identity

    keychain = signing_identity.keychain_path(env.app_dir)
    print(f"identity keychain: {keychain} ({'present' if keychain.exists() else 'MISSING'})")
    print(f"keychain password: {signing_identity.password_path(env.app_dir)}")
    identities = subprocess.run(  # noqa: S603
        ["/usr/bin/security", "find-identity", "-p", "codesigning", str(keychain)],
        capture_output=True,
        text=True,
        check=False,
    )
    print((identities.stdout or "").strip() or "(no identities)")
    app = env.app_dir / "MeivaPermissionsHelper.app"
    if not app.exists():
        print(f"installed helper: absent ({app})")
        return 0
    print(f"installed helper: {app}")
    for line in _codesign("-d", "-r-", app=app).splitlines():
        if line.startswith(("designated =>", "# designated =>")):
            print(f"  {line}")
    signature = [
        line
        for line in _codesign("-dvvv", app=app).splitlines()
        if line.startswith(("Signature=", "Authority=", "TeamIdentifier="))
    ]
    print("  " + "\n  ".join(signature))
    return 0


def provision(env: BuildEnv) -> int:
    from services.desktop.permissions_helper import lifecycle, signing_identity

    run_tool = partial(lifecycle.run_tool, run=env.run)
    signing_identity.ensure(run_tool, create=True, app_dir=env.app_dir, rng=env.rng)
    print(f"signing identity ready: {signing_identity.keychain_path(env.app_dir)}")
    print(f"record: {signing_identity.readme_path(env.app_dir)}")
    return status(env)


def export(path: str, env: BuildEnv) -> int:
    from services.desktop.permissions_helper import lifecycle, signing_identity

    run_tool = partial(lifecycle.run_tool, run=env.run)
    signing_identity.ensure(run_tool, create=False, app_dir=env.app_dir, rng=env.rng)
    target = Path(path).expanduser().resolve()
    password = signing_identity.password_path(env.app_dir).read_text().strip()
    run_tool(
        [
            "/usr/bin/security",
            "export",
            "-t",
            "identities",
            "-f",
            "pkcs12",
            "-k",
            str(signing_identity.keychain_path(env.app_dir)),
            "-P",
            password,
            "-o",
            str(target),
        ]
    )
    target.chmod(0o600)
    print(
        f"exported the identity to {target} "
        f"(password: {signing_identity.password_path(env.app_dir)})"
    )
    return 0


def import_identity(path: str, *, password_file: str | None, env: BuildEnv) -> int:
    from services.desktop.permissions_helper import lifecycle, signing_identity

    run_tool = partial(lifecycle.run_tool, run=env.run)
    archive = Path(path).expanduser().resolve()
    source = (
        Path(password_file).expanduser()
        if password_file
        else signing_identity.password_path(env.app_dir)
    )
    if not source.exists():
        raise SystemExit(f"no keychain password at {source}")
    password = source.read_text().strip()
    if password_file:
        signing_identity.write_secret(signing_identity.password_path(env.app_dir), password)
    keychain = signing_identity.keychain_path(env.app_dir)
    if not keychain.exists():
        env.app_dir.mkdir(parents=True, exist_ok=True)
        run_tool(["/usr/bin/security", "create-keychain", "-p", password, str(keychain)])
        run_tool(["/usr/bin/security", "set-keychain-settings", str(keychain)])
    run_tool(["/usr/bin/security", "unlock-keychain", "-p", password, str(keychain)])
    run_tool(
        [
            "/usr/bin/security",
            "import",
            str(archive),
            "-k",
            str(keychain),
            "-P",
            password,
            "-T",
            "/usr/bin/codesign",
            "-T",
            "/usr/bin/security",
        ]
    )
    run_tool(
        [
            "/usr/bin/security",
            "set-key-partition-list",
            "-S",
            "apple-tool:,apple:",
            "-s",
            "-k",
            password,
            str(keychain),
        ]
    )
    signing_identity.require(run_tool, app_dir=env.app_dir)
    print(f"imported the identity into {keychain}")
    return status(env)


def switch(*, env: BuildEnv, start: bool) -> int:
    from services.desktop.permissions_helper import lifecycle, signing_identity

    home = env.app_dir
    app = home / "MeivaPermissionsHelper.app"
    state = home / "build-state.json"
    backup = home / f"backup-{env.clock.now().strftime('%Y%m%dT%H%M%SZ')}"
    before_dr = _codesign("-d", "-r-", app=app) if app.exists() else ""
    if app.exists():
        backup.mkdir(parents=True, exist_ok=False)
        shutil.copytree(app, backup / app.name, symlinks=True)
        if state.exists():
            shutil.copy2(state, backup / state.name)
        (backup / "codesign-dvvv.txt").write_text(_codesign("-dvvv", app=app))
        (backup / "codesign-dr.txt").write_text(before_dr)
        print(f"backed up the installed helper to {backup}")

    # meiva:note
    # Every failure of the swap restores the backup: this runs detached on a host
    # whose runtime it just stopped, so nobody is watching the terminal.
    try:
        signing_identity.ensure(
            partial(lifecycle.run_tool, run=env.run), create=True, app_dir=env.app_dir, rng=env.rng
        )
        built, rebuilt = lifecycle.build_and_sign(destination=None, env=env)
    except Exception as error:
        print(f"switch failed: {error}", file=sys.stderr)
        _restore(backup, home, app, before_dr)
        return 1

    after = _codesign("-d", "-r-", app=built)
    requirement = next(
        (
            line.removeprefix("# ").removeprefix("designated => ")
            for line in after.splitlines()
            if line.startswith(("designated =>", "# designated =>"))
        ),
        "",
    )
    if "certificate" not in requirement:
        print(f"the rebuilt helper is not certificate-signed: {after}", file=sys.stderr)
        _restore(backup, home, app, before_dr)
        return 1
    print(f"rebuilt: {rebuilt}")
    print(f"designated requirement: {requirement}")
    for line in _codesign("-dvvv", app=built).splitlines():
        if line.startswith(("Signature=", "Authority=", "TeamIdentifier=", "CDHash=")):
            print(f"  {line}")
    print(
        "\nNext: `meiva start` to load the helper, then grant Screen Recording, "
        "Accessibility, AppleEvents and App Management in System Settings -- this is a new\n"
        "code identity, so those grants are asked again exactly once."
    )
    if start:
        _start_services()
    return 0


# meiva:note
# Bring this home back up after the switch retired it, through the checkout's own
# entry point: `meiva start` needs an explicit service selection.
def _start_services() -> None:
    meiva = Path(sys.executable).parent / "meiva"
    print(f"starting this home's services: {meiva} start --all-services")
    result = subprocess.run(  # noqa: S603
        [str(meiva), "start", "--all-services"], check=False
    )
    print(f"`meiva start --all-services` exited {result.returncode}")


def _restore(backup: Path, home: Path, app: Path, before_dr: str) -> None:
    saved = backup / app.name
    if not saved.exists():
        print("nothing to restore: this host had no installed helper", file=sys.stderr)
        return
    shutil.rmtree(app, ignore_errors=True)
    shutil.copytree(saved, app, symlinks=True)
    for name in ("build-state.json",):
        if (backup / name).exists():
            shutil.copy2(backup / name, home / name)
    restored = _codesign("-d", "-r-", app=app)
    print(f"restored the backup from {backup}")
    print(restored.strip())
    if restored.strip() != before_dr.strip():
        print("WARNING: the restored requirement differs from the pre-switch one", file=sys.stderr)


# meiva:note
# This host's helper facts, one `key=value` per line, for a caller that compares machines
# without importing this package: a wave gate reads them as opaque strings, and only this
# module decides what "stale", "loaded" and the certificate root mean.
def facts(env: BuildEnv) -> int:
    from services.desktop.permissions_helper import lifecycle
    from services.desktop.permissions_helper.launchd_job import helper_job_loaded
    from services.desktop.permissions_helper.lifecycle import PermissionsHelperBuildError

    app = env.app_dir / "MeivaPermissionsHelper.app"
    try:
        expected = lifecycle.source_content_hash()
    except OSError:
        expected = "-"
    print(f"expected_hash={expected}")
    record = lifecycle.installed_build_state(env.app_dir)
    recorded = record["source_hash"] if record is not None else "-"
    signed_at = record["signed_at"] if record is not None else "-"
    print(f"recorded_hash={recorded}")
    print(f"signed_at={signed_at}")
    if not app.exists():
        print("helper=absent")
        return 0
    print("helper=present")
    print(f"valid={'yes' if lifecycle.is_valid_app(app, run=env.run) else 'no'}")
    loaded = helper_job_loaded(env.home, facts=env.facts, run=env.run, timers=env.timers)
    print(f"loaded={'yes' if loaded else 'no'}")
    print(f"stale={'no' if record is not None and recorded == expected else 'yes'}")
    root = "-"
    try:
        match = _CERT_ROOT.search(lifecycle.read_dr(app, run=env.run))
    except PermissionsHelperBuildError as error:
        print(f"facts: {error}", file=sys.stderr)
    else:
        root = match[1] if match is not None else "-"
    print(f"cert_root={root}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument(
        "command", choices=("status", "facts", "provision", "import", "export", "switch")
    )
    parser.add_argument("path", nargs="?", help="PKCS#12 archive for import/export")

    parser.add_argument(
        "--password-file",
        help="keychain password that opens an archive being imported (default: this home's)",
    )
    parser.add_argument(
        "--start", action="store_true", help="run `meiva start --all-services` after the switch"
    )
    parser.add_argument(
        "--detach",
        action="store_true",
        help="re-run this command in its own session, so the switch survives the runtime it stops",
    )
    args = parser.parse_args(argv)
    from base.paths.home import resolve_meiva_home

    cluster_home = resolve_meiva_home()
    if args.detach:
        return _detach(sys.argv[1:], cluster_home)
    if args.command in {"import", "export"} and not args.path:
        parser.error(f"{args.command} needs a PKCS#12 path")
    env = _build_env(cluster_home)
    return {
        "status": lambda: status(env),
        "facts": lambda: facts(env),
        "provision": lambda: provision(env),
        "switch": lambda: switch(env=env, start=args.start),
        "export": lambda: export(args.path or "", env),
        "import": lambda: import_identity(
            (args.path or ""), password_file=args.password_file, env=env
        ),
    }[args.command]()


# meiva:note
# The one world every command of this script works in, read once here: the home, the installed
# helper directory, the launchd facts of this account, the command runner, the timers and the
# clock, and the operating system's random generator the signing password is drawn from.
def _build_env(cluster_home: Path) -> BuildEnv:
    from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
    from base.host.system.hostport import Host
    from services.desktop.permissions_helper.launchd_job import LaunchdFacts

    host = Host.system()
    return BuildEnv(
        home=cluster_home,
        facts=LaunchdFacts.of(host, user_home=Path.home()),
        run=host.run,
        timers=SYSTEM_TIMERS,
        clock=Clock(
            ClockConfig("UTC", None, message_timestamp_weekday=False), timers=SYSTEM_TIMERS
        ),
        rng=random.SystemRandom(),
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
