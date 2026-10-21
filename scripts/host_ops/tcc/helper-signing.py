#!/usr/bin/env python3
"""The permissions helper's signing identity: inspect it, provision it, switch the install.

Run from a checkout with its own `.venv`:

    .venv/bin/python scripts/host_ops/tcc/helper-signing.py status
    .venv/bin/python scripts/host_ops/tcc/helper-signing.py switch

`switch` is the whole install-location change: back the installed helper up,
rebuild it signed with the fixed identity, prove the designated requirement now
names the certificate, and restore the backup if any step fails.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


# meiva:note
# `MEIVA_HOME` is read when the app modules import, so it must be set before
# they do -- including for `--home`, which is how a rehearsal runs against a
# throwaway home instead of the running deployment.
def _select_home(home: str | None) -> None:
    if home:
        os.environ["MEIVA_HOME"] = str(Path(home).expanduser().resolve())


def _codesign(*args: str, app: Path) -> str:
    result = subprocess.run(  # noqa: S603 - fixed argv, macOS tool
        ["/usr/bin/codesign", *args, str(app)], capture_output=True, text=True, check=False
    )
    return (result.stdout or "") + (result.stderr or "")


def status() -> int:
    from services.desktop.permissions_helper import signing_identity

    keychain = signing_identity.keychain_path()
    print(f"identity keychain: {keychain} ({'present' if keychain.exists() else 'MISSING'})")
    print(f"keychain password: {signing_identity.password_path()}")
    identities = subprocess.run(  # noqa: S603 - fixed argv, macOS tool
        ["/usr/bin/security", "find-identity", "-p", "codesigning", str(keychain)],
        capture_output=True,
        text=True,
        check=False,
    )
    print((identities.stdout or "").strip() or "(no identities)")
    from base.paths import permissions_helper_app_dir

    app = permissions_helper_app_dir() / "MeivaPermissionsHelper.app"
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


def provision() -> int:
    from services.desktop.permissions_helper import lifecycle, signing_identity

    signing_identity.ensure(lifecycle.run_tool, create=True)
    print(f"signing identity ready: {signing_identity.keychain_path()}")
    print(f"record: {signing_identity.readme_path()}")
    return status()


def export(path: str) -> int:
    from services.desktop.permissions_helper import lifecycle, signing_identity

    signing_identity.ensure(lifecycle.run_tool, create=False)
    target = Path(path).expanduser().resolve()
    password = signing_identity.password_path().read_text().strip()
    lifecycle.run_tool(
        [
            "/usr/bin/security",
            "export",
            "-t",
            "identities",
            "-f",
            "pkcs12",
            "-k",
            str(signing_identity.keychain_path()),
            "-P",
            password,
            "-o",
            str(target),
        ]
    )
    target.chmod(0o600)
    print(f"exported the identity to {target} (password: {signing_identity.password_path()})")
    return 0


def import_identity(path: str) -> int:
    from services.desktop.permissions_helper import lifecycle, signing_identity

    archive = Path(path).expanduser().resolve()
    if not signing_identity.password_path().exists():
        raise SystemExit(f"no keychain password at {signing_identity.password_path()}")
    password = signing_identity.password_path().read_text().strip()
    keychain = signing_identity.keychain_path()
    if not keychain.exists():
        signing_identity.directory().mkdir(parents=True, exist_ok=True)
        lifecycle.run_tool(["/usr/bin/security", "create-keychain", "-p", password, str(keychain)])
        lifecycle.run_tool(["/usr/bin/security", "set-keychain-settings", str(keychain)])
    lifecycle.run_tool(["/usr/bin/security", "unlock-keychain", "-p", password, str(keychain)])
    lifecycle.run_tool(
        ["/usr/bin/security", "import", str(archive), "-k", str(keychain), "-P", password,
         "-T", "/usr/bin/codesign", "-T", "/usr/bin/security"]
    )
    lifecycle.run_tool(
        ["/usr/bin/security", "set-key-partition-list", "-S", "apple-tool:,apple:", "-s",
         "-k", password, str(keychain)]
    )
    signing_identity.require(lifecycle.run_tool)
    print(f"imported the identity into {keychain}")
    return status()


def switch(*, retire: bool) -> int:
    from base.paths import permissions_helper_app_dir
    from services.desktop.permissions_helper import lifecycle, signing_identity

    home = permissions_helper_app_dir()
    app = home / "MeivaPermissionsHelper.app"
    state = home / "build-state.json"
    backup = home / f"backup-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    before_dr = _codesign("-d", "-r-", app=app) if app.exists() else ""
    if app.exists():
        backup.mkdir(parents=True, exist_ok=False)
        shutil.copytree(app, backup / app.name, symlinks=True)
        if state.exists():
            shutil.copy2(state, backup / state.name)
        (backup / "codesign-dvvv.txt").write_text(_codesign("-dvvv", app=app))
        (backup / "codesign-dr.txt").write_text(before_dr)
        print(f"backed up the installed helper to {backup}")

    if retire:
        _retire_helper()
    try:
        signing_identity.ensure(lifecycle.run_tool, create=True)
        built, rebuilt = lifecycle.build_and_sign()
    except Exception as error:  # noqa: BLE001 - every failure restores the backup
        print(f"switch failed: {error}", file=sys.stderr)
        _restore(backup, home, app, before_dr)
        return 1

    after = _codesign("-d", "-r-", app=built)
    requirement = next(
        (line.removeprefix("# ").removeprefix("designated => ") for line in after.splitlines()
         if line.startswith(("designated =>", "# designated =>"))),
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
    return 0


def _retire_helper() -> None:
    """Stop this home's helper job, which is also the parent of its meiva-root.

    A loaded helper is never replaced in place, so this is the step that turns
    the swap into a restart of everything this home supervises on the host:
    agents die with it and `meiva start` brings them back.
    """
    from cli.commands.lifecycle._stop_extras import stop_permissions_helper

    print("retiring the running permissions helper (and the meiva-root it owns) ...")
    stop_permissions_helper(timeout_s=60.0)


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


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("command", choices=("status", "provision", "import", "export", "switch"))
    parser.add_argument("path", nargs="?", help="PKCS#12 archive for import/export")
    parser.add_argument("--home", help="MEIVA_HOME to act on, e.g. a rehearsal home")
    parser.add_argument(
        "--retire",
        action="store_true",
        help="stop the running helper first; this also stops every agent this home supervises",
    )
    args = parser.parse_args(argv)
    _select_home(args.home)
    if args.command in {"import", "export"} and not args.path:
        parser.error(f"{args.command} needs a PKCS#12 path")
    return {
        "status": status,
        "provision": provision,
        "switch": lambda: switch(retire=args.retire),
        "export": lambda: export(args.path or ""),
        "import": lambda: import_identity(args.path or ""),
    }[args.command]()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
