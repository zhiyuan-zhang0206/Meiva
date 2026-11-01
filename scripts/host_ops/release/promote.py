#!/usr/bin/env python3
# meiva:note
# Verify a certified `meiva-release/v1` release tag and advance the `channel/stable` annotated tag
# pointer without re-running tests or opening reverse pull requests.
# meiva:link docs/conventions/engineering/host-versioning.md

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# meiva:link scripts/docs/scripts.meiva.okf.md
from cli.fleet_update.plan.release_target import (  # noqa: E402
    ChannelPointer,
    format_tag_message,
    parse_release_tag,
)
from scripts.host_ops.release.git_tags import (  # noqa: E402
    append_github_output,
    parse_utc_timestamp,
    read_verified_certificate,
    write_annotated_tag,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Advance channel/stable to a verified certified release tag."
    )
    parser.add_argument("--repo", default=".", type=Path)
    parser.add_argument(
        "--version",
        required=True,
        help="certified CalVer tag (vYYYY.MM.DD.HHMMSS[-hfN]) to promote to channel/stable",
    )
    parser.add_argument("--updated-at", required=True, help="ISO-8601 UTC promotion timestamp")
    parser.add_argument("--reason", default="", help="operator note recorded in channel/stable")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--github-output", default=None, type=Path)
    args = parser.parse_args(argv)

    version = args.version.strip()
    if parse_release_tag(version) is None:
        err = f"{version!r} is not a CalVer release tag (vYYYY.MM.DD.HHMMSS[-hfN])"
        print(f"::error::{err}", file=sys.stderr)
        print(json.dumps({"ok": False, "error": err}))
        return 2
    try:
        _dt, updated_iso = parse_utc_timestamp(args.updated_at, "--updated-at")
    except ValueError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2

    cert, reason = read_verified_certificate(args.repo, version)
    if cert is None:
        print(f"::error::refused to promote {version}: {reason}", file=sys.stderr)
        print(json.dumps({"ok": False, "error": reason, "version": version}))
        return 2

    pointer = ChannelPointer(
        channel="stable",
        version=cert.version,
        sha=cert.sha,
        updated_at=updated_iso,
        reason=args.reason.strip(),
    )
    channel_ref = "channel/stable"
    if not args.dry_run:
        try:
            write_annotated_tag(
                args.repo,
                channel_ref,
                cert.sha,
                format_tag_message(pointer),
                force=True,
            )
        except RuntimeError as exc:
            print(f"::error::{exc}", file=sys.stderr)
            print(json.dumps({"ok": False, "error": str(exc)}))
            return 1

    append_github_output(
        args.github_output,
        {
            "channel_tag": channel_ref,
            "version": cert.version,
            "sha": cert.sha,
        },
    )
    print(
        json.dumps(
            {
                "ok": True,
                "channel": "stable",
                "channel_tag": channel_ref,
                "version": cert.version,
                "sha": cert.sha,
                "gate_run_url": cert.gate_run_url,
                "dry_run": bool(args.dry_run),
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
