#!/usr/bin/env python3
# meiva:note
# Precheck and mint Git-native `meiva-release/v1` release certificates and advance `channel/latest`
# on `main` Release Train certifications without opening reverse pull requests.
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
    ReleaseCertificate,
    format_tag_message,
    mint_release_tag,
    parse_release_tag,
)
from scripts.host_ops.release import tag as release_tag  # noqa: E402
from scripts.host_ops.release.git_tags import (  # noqa: E402
    append_github_output,
    is_ancestor,
    list_tag_names,
    parse_utc_timestamp,
    read_verified_certificate,
    read_verified_channel,
    resolved_commit,
    write_annotated_tag,
)


def _resolve_target_ref(repository: Path, event_name: str, target_ref: str) -> tuple[str, str]:
    if event_name == "schedule":
        for candidate in ("refs/remotes/origin/main", "refs/heads/main", "HEAD"):
            sha = resolved_commit(repository, candidate)
            if sha is not None:
                return sha, "refs/heads/main"
        raise ValueError(f"cannot resolve main in {repository}")
    ref = target_ref.strip()
    if not ref:
        raise ValueError("--target-ref cannot be empty")
    if ref.startswith("refs/"):
        sha = resolved_commit(repository, ref)
        if sha is None:
            raise ValueError(f"target ref {ref!r} does not resolve to a commit in {repository}")
        return sha, ref
    for remote_or_local in (f"refs/remotes/origin/{ref}", f"refs/heads/{ref}"):
        sha = resolved_commit(repository, remote_or_local)
        if sha is not None:
            return sha, f"refs/heads/{ref}"
    sha = resolved_commit(repository, ref)
    if sha is None:
        raise ValueError(f"target ref {ref!r} does not resolve to a commit in {repository}")
    return sha, ref


def _validate_hotfix_base(repository: Path, hotfix_of: str, target_sha: str) -> None:
    parsed = parse_release_tag(hotfix_of)
    if parsed is None or parsed.hotfix != 0:
        raise ValueError(
            f"--hotfix-of {hotfix_of!r} must be a non-hotfix CalVer tag (vYYYY.MM.DD.HHMMSS)"
        )
    base_cert, reason = read_verified_certificate(repository, hotfix_of)
    if base_cert is None:
        raise ValueError(f"--hotfix-of {hotfix_of} is not a certified release: {reason}")
    if not is_ancestor(repository, base_cert.sha, target_sha):
        raise ValueError(
            f"target {target_sha[:12]} does not descend from --hotfix-of {hotfix_of} "
            f"({base_cert.sha[:12]})"
        )


def _validate_explicit_version(
    repository: Path, version: str, hotfix_of: str, target_sha: str
) -> None:
    parsed = parse_release_tag(version)
    if parsed is None:
        raise ValueError(
            f"--version {version!r} must be a CalVer release tag (vYYYY.MM.DD.HHMMSS[-hfN])"
        )
    if hotfix_of:
        if parsed.hotfix < 1 or parsed.base_tag != hotfix_of:
            raise ValueError(
                f"--version {version!r} must be a hotfix of {hotfix_of} ({hotfix_of}-hfN)"
            )
    elif parsed.hotfix != 0:
        raise ValueError(f"hotfix tag {version!r} requires --hotfix-of {parsed.base_tag}")
    existing = release_tag.tagged_commit(repository, version)
    if release_tag.plan(existing, target_sha) == "refuse":
        raise ValueError(f"tag {version} already marks {existing}, not {target_sha}")


def _run_precheck(args: argparse.Namespace) -> int:
    if args.event_name not in ("schedule", "workflow_dispatch"):
        raise ValueError(f"unsupported --event-name {args.event_name!r}")
    sha, source_ref = _resolve_target_ref(args.repo, args.event_name, args.target_ref)
    hotfix_of = args.hotfix_of.strip()
    version = args.version.strip()
    if hotfix_of:
        _validate_hotfix_base(args.repo, hotfix_of, sha)
    if version:
        _validate_explicit_version(args.repo, version, hotfix_of, sha)
    if args.event_name == "schedule":
        pointer, cert, _reason = read_verified_channel(args.repo, "latest")
        if pointer is not None and cert is not None and cert.sha == sha:
            outputs = {
                "skip_train": "true",
                "sha": sha,
                "source_ref": source_ref,
                "existing_version": cert.version,
                "version": "",
                "hotfix_of": "",
            }
            append_github_output(args.github_output, outputs)
            print(json.dumps({"ok": True, **outputs}))
            return 0
    outputs = {
        "skip_train": "false",
        "sha": sha,
        "source_ref": source_ref,
        "existing_version": "",
        "version": version,
        "hotfix_of": hotfix_of,
    }
    append_github_output(args.github_output, outputs)
    print(json.dumps({"ok": True, **outputs}))
    return 0


def _should_advance_latest(repository: Path, source_ref: str, hotfix_of: str, sha: str) -> bool:
    if source_ref != "refs/heads/main" or bool(hotfix_of):
        return False
    pointer, cert, _reason = read_verified_channel(repository, "latest")
    if pointer is None or cert is None:
        return True
    return is_ancestor(repository, cert.sha, sha)


def _select_version(
    repository: Path, explicit_version: str, hotfix_of: str, sha: str, certified_dt: object
) -> str:
    if explicit_version:
        _validate_explicit_version(repository, explicit_version, hotfix_of, sha)
        return explicit_version
    from datetime import datetime

    if not isinstance(certified_dt, datetime):
        raise TypeError("certified_dt must be a datetime")
    if hotfix_of:
        return mint_release_tag(
            certified_dt, hotfix_of=hotfix_of, existing_tags=list_tag_names(repository)
        )
    return mint_release_tag(certified_dt)


def _apply_release_tag(repository: Path, cert: ReleaseCertificate) -> str:
    existing = release_tag.tagged_commit(repository, cert.version)
    tag_plan = release_tag.plan(existing, cert.sha)
    if tag_plan == "refuse":
        raise ValueError(f"tag {cert.version} already marks {existing}, not {cert.sha}")
    if tag_plan == "skip":
        verified, reason = read_verified_certificate(repository, cert.version)
        if verified is None:
            raise ValueError(f"existing tag {cert.version} is not a valid certificate: {reason}")
        return tag_plan
    write_annotated_tag(repository, cert.version, cert.sha, format_tag_message(cert))
    return tag_plan


def _run_mint(args: argparse.Namespace) -> int:
    sha = resolved_commit(args.repo, args.sha.strip())
    if sha is None or sha != args.sha.strip():
        raise ValueError(f"--sha {args.sha!r} must be an existing 40-hex commit in {args.repo}")
    certified_dt, certified_iso = parse_utc_timestamp(args.certified_at, "--certified-at")
    source_ref = args.source_ref.strip()
    gate_run_id = args.gate_run_id.strip()
    gate_run_url = args.gate_run_url.strip()
    if not source_ref or not gate_run_id or not gate_run_url:
        raise ValueError("--source-ref, --gate-run-id and --gate-run-url are required")
    hotfix_of = args.hotfix_of.strip()
    if hotfix_of:
        _validate_hotfix_base(args.repo, hotfix_of, sha)
    version = _select_version(args.repo, args.version.strip(), hotfix_of, sha, certified_dt)
    cert = ReleaseCertificate(
        version=version,
        sha=sha,
        source_ref=source_ref,
        gate_run_id=gate_run_id,
        gate_run_url=gate_run_url,
        certified_at=certified_iso,
        hotfix_of=hotfix_of or None,
        reason=args.reason.strip(),
    )
    tag_plan = _apply_release_tag(args.repo, cert)
    advance_latest = _should_advance_latest(args.repo, source_ref, hotfix_of, sha)
    if advance_latest:
        pointer = ChannelPointer(
            channel="latest",
            version=version,
            sha=sha,
            updated_at=certified_iso,
            reason=args.reason.strip(),
        )
        write_annotated_tag(
            args.repo,
            "channel/latest",
            sha,
            format_tag_message(pointer),
            force=True,
        )
    outputs = {
        "version": version,
        "sha": sha,
        "tag_action": tag_plan,
        "advance_latest": "true" if advance_latest else "false",
    }
    append_github_output(args.github_output, outputs)
    print(json.dumps({"ok": True, **outputs}))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Precheck and mint Git-native Meiva release certificates."
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    pre = sub.add_parser("precheck", help="resolve target ref and check idle-train skip")
    pre.add_argument("--repo", default=".", type=Path)
    pre.add_argument("--event-name", default="workflow_dispatch")
    pre.add_argument("--target-ref", default="main")
    pre.add_argument("--version", default="")
    pre.add_argument("--hotfix-of", default="")
    pre.add_argument("--github-output", default=None, type=Path)

    mint = sub.add_parser("mint", help="mint annotated release tag and update channel/latest")
    mint.add_argument("--repo", default=".", type=Path)
    mint.add_argument("--sha", required=True)
    mint.add_argument("--source-ref", required=True)
    mint.add_argument("--certified-at", required=True)
    mint.add_argument("--gate-run-id", required=True)
    mint.add_argument("--gate-run-url", required=True)
    mint.add_argument("--version", default="")
    mint.add_argument("--hotfix-of", default="")
    mint.add_argument("--reason", default="")
    mint.add_argument("--github-output", default=None, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.subcommand == "precheck":
            return _run_precheck(args)
        return _run_mint(args)
    except (ValueError, RuntimeError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
