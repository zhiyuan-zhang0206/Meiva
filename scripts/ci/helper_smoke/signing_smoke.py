from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from base.clock import SYSTEM_TIMERS
from base.sessions.helperproc import helper_client
from services.desktop.permissions_helper import lifecycle

APPLE_EVENTS_ENTITLEMENT = "com.apple.security.automation.apple-events"


def readback_failure(what: str, returncode: int, text: str, expected: str) -> str | None:
    """Why a `codesign -d` readback does not show `expected`, or None when it does."""
    if returncode != 0:
        return f"{what} readback failed: {text[:500]}"
    if expected not in text:
        return f"{what} readback has no {expected!r}: {text[:500]}"
    return None


def _codesign_readback(*args: str) -> tuple[int, str]:
    result = subprocess.run(  # noqa: S603 - fixed argv, macOS tool
        ["codesign", *args], capture_output=True, check=False, text=True
    )
    return result.returncode, (result.stdout or "") + (result.stderr or "")


# meiva:note
# Read the designated requirement and the entitlement back through the real `codesign`.
#
# An ad-hoc signature names its code hash in the designated requirement. The entitlement must land in the signature, not only on the command line.
def verify_signature(app: Path) -> None:
    returncode, dr_text = _codesign_readback("-d", "-r-", str(app))
    print(f"DR readback: {dr_text[:500]}")
    problem = readback_failure("designated requirement", returncode, dr_text, "cdhash")
    if problem is None:
        returncode, ent_text = _codesign_readback("-d", "--entitlements", ":-", str(app))
        problem = readback_failure("entitlements", returncode, ent_text, APPLE_EVENTS_ENTITLEMENT)
    if problem is not None:
        raise SystemExit(problem)


def load_and_ping(app: Path) -> None:
    """Bootstrap the helper under launchd and ping it.

    A bootstrap failure intentionally fails the lane: the hosted runner's user launchd chain is
    part of the production contract being tested.
    """
    lifecycle.install_and_load(app, rebuilt=True, helper=helper_client, timers=SYSTEM_TIMERS)
    if helper_client().ping()["pong"] is not True:
        raise SystemExit("the helper did not answer the ping")


def run() -> None:
    app, _rebuilt = lifecycle.build_and_sign()
    verify_signature(app)
    load_and_ping(app)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Build, ad-hoc sign, launch and ping the real permissions helper."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="build, sign, read back, launch and ping")
    commands.add_parser("label", help="print the launchd label of the helper job")
    args = parser.parse_args(argv)
    if args.command == "label":
        print(lifecycle.label())
    else:
        run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
