# meiva:note
# Shared Git tag and commit queries/mutations for `scripts/host_ops/release/*`. Fixed argv only
# (never `shell=True`), delegating all certificate and channel parsing to
# `cli.fleet_update.plan.release_target`.
# meiva:link docs/conventions/engineering/host-versioning.md

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from cli.fleet_update.plan.release_target import (
    Channel,
    ChannelPointer,
    ReleaseCertificate,
    TargetRequest,
    TargetSnapshot,
    parse_channel_pointer,
    parse_tag_certificate,
    resolve_update_target,
)

_TAGGER_ARGS = (
    "-c",
    "user.name=github-actions[bot]",
    "-c",
    "user.email=41898282+github-actions[bot]@users.noreply.github.com",
)


def git_run(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ("git", "-C", str(repository), *arguments),
        capture_output=True,
        text=True,
        check=False,
    )


def resolved_commit(repository: Path, revision: str) -> str | None:
    done = git_run(repository, "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}")
    sha = done.stdout.strip()
    return sha if done.returncode == 0 and sha else None


def is_ancestor(repository: Path, ancestor: str, descendant: str) -> bool:
    return git_run(repository, "merge-base", "--is-ancestor", ancestor, descendant).returncode == 0


def list_tag_names(repository: Path) -> list[str]:
    done = git_run(repository, "tag", "--list")
    if done.returncode != 0:
        return []
    return [line.strip() for line in done.stdout.splitlines() if line.strip()]


def read_tag_annotation(repository: Path, tag_name: str) -> str | None:
    ref = f"refs/tags/{tag_name}"
    obj_type = git_run(repository, "cat-file", "-t", ref)
    if obj_type.returncode != 0 or obj_type.stdout.strip() != "tag":
        return None
    payload = git_run(repository, "cat-file", "-p", ref)
    if payload.returncode != 0:
        return None
    _headers, sep, body = payload.stdout.partition("\n\n")
    return body if sep else None


def tags_pointing_at(repository: Path, commit_sha: str) -> dict[str, str | None]:
    done = git_run(repository, "tag", "--points-at", commit_sha)
    if done.returncode != 0:
        return {}
    return {
        raw.strip(): read_tag_annotation(repository, raw.strip())
        for raw in done.stdout.splitlines()
        if raw.strip()
    }


def read_verified_certificate(
    repository: Path, tag_name: str
) -> tuple[ReleaseCertificate | None, str]:
    peeled = resolved_commit(repository, f"refs/tags/{tag_name}")
    if peeled is None:
        return None, f"release tag {tag_name} does not exist in {repository}"
    body = read_tag_annotation(repository, tag_name)
    return parse_tag_certificate(tag_name, peeled, body)


def read_verified_channel(
    repository: Path, channel: Channel
) -> tuple[ChannelPointer | None, ReleaseCertificate | None, str]:
    tag_ref = f"channel/{channel}"
    peeled = resolved_commit(repository, f"refs/tags/{tag_ref}")
    if peeled is None:
        return None, None, f"{tag_ref} does not exist in {repository}"
    body = read_tag_annotation(repository, tag_ref)
    pointer, reason = parse_channel_pointer(channel, peeled, body)
    if pointer is None:
        return None, None, reason
    cert, cert_reason = read_verified_certificate(repository, pointer.version)
    if cert is None:
        return pointer, None, f"{tag_ref} target {pointer.version} is uncertified: {cert_reason}"
    if cert.sha != peeled:
        return (
            pointer,
            None,
            f"{tag_ref} peels to {peeled[:12]}, but {pointer.version} marks {cert.sha[:12]}",
        )
    return pointer, cert, "verified"


def read_certified_commit(
    repository: Path, commit_sha: str
) -> tuple[ReleaseCertificate | None, str]:
    resolved, reason = resolve_update_target(
        TargetRequest(target_mode="commit", commit_rev=commit_sha),
        TargetSnapshot(
            repo_display=str(repository),
            peeled_sha=commit_sha,
            candidate_tags=tags_pointing_at(repository, commit_sha),
        ),
    )
    if resolved is None or resolved.certificate is None:
        return None, reason
    return resolved.certificate, reason


def write_annotated_tag(
    repository: Path,
    tag_name: str,
    target_sha: str,
    message: str,
    *,
    force: bool = False,
) -> None:
    force_args = ("-f",) if force else ()
    done = subprocess.run(  # noqa: S603
        (
            "git",
            *_TAGGER_ARGS,
            "-C",
            str(repository),
            "tag",
            *force_args,
            "-a",
            tag_name,
            "-m",
            message,
            target_sha,
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        detail = done.stderr.strip() or done.stdout.strip() or f"exit {done.returncode}"
        raise RuntimeError(f"failed to write annotated tag {tag_name}: {detail}")


def parse_utc_timestamp(raw: str, flag_name: str) -> tuple[datetime, str]:
    normalized = raw.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{flag_name} {raw!r} is not a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{flag_name} {raw!r} must include an explicit timezone")
    utc = parsed.astimezone(UTC).replace(microsecond=0)
    return utc, utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def append_github_output(output_path: Path | None, values: Mapping[str, str]) -> None:
    if output_path is None:
        return
    chunks: list[str] = []
    for key, value in values.items():
        if "\n" in value:
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
            marker = f"MEIVA_EOF_{digest}"
            chunks.append(f"{key}<<{marker}\n{value}\n{marker}\n")
        else:
            chunks.append(f"{key}={value}\n")
    with output_path.open("a", encoding="utf-8") as handle:
        handle.write("".join(chunks))
