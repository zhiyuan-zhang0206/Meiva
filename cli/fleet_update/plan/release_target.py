# meiva:note
# What makes a commit releasable to a fleet window: the UTC CalVer tag grammar
# (`vYYYY.MM.DD.HHMMSS` and `-hf<N>`), the Git-native `meiva-release/v1` tag annotation
# certificate, the `channel/stable` and `channel/latest` moving pointers, and the three
# target-resolution modes (`--channel`, `--release` / certified `--new`, and `--track-branch`).
# Values in, decisions out: stdlib only, no repository or network reads.
# meiva:link docs/conventions/engineering/host-versioning.md

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, TypeGuard

CERTIFICATE_SCHEMA = "meiva-release/v1"
CHANNEL_SCHEMA = "meiva-channel/v1"
BEGIN_CERTIFICATE = "-----BEGIN MEIVA RELEASE CERTIFICATE-----"
END_CERTIFICATE = "-----END MEIVA RELEASE CERTIFICATE-----"
BEGIN_CHANNEL = "-----BEGIN MEIVA CHANNEL POINTER-----"
END_CHANNEL = "-----END MEIVA CHANNEL POINTER-----"

Channel = Literal["stable", "latest"]
CHANNELS: tuple[Channel, ...] = ("stable", "latest")
TargetMode = Literal["channel", "release", "track-branch", "commit"]

_CALVER_TAG = re.compile(r"^v(\d{4})\.(\d{2})\.(\d{2})\.(\d{2})(\d{2})(\d{2})(?:-hf([1-9]\d*))?$")
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


# meiva:note
# Exact-second UTC CalVer (`vYYYY.MM.DD.HHMMSS`) with optional post-release hotfix ordinal
# (`-hf1`, `-hf2`, ...). `hotfix=0` is the base release so `-hf1` sorts immediately after its base
# and before any chronologically later release.
@dataclass(frozen=True, order=True)
class ReleaseVersion:
    year: int
    month: int
    day: int
    hour: int
    minute: int
    second: int
    hotfix: int = 0

    @property
    def base_tag(self) -> str:
        return (
            f"v{self.year:04d}.{self.month:02d}.{self.day:02d}."
            f"{self.hour:02d}{self.minute:02d}{self.second:02d}"
        )

    @property
    def tag(self) -> str:
        if self.hotfix > 0:
            return f"{self.base_tag}-hf{self.hotfix}"
        return self.base_tag


@dataclass(frozen=True)
class ReleaseCertificate:
    version: str
    sha: str
    source_ref: str
    gate_run_id: str
    gate_run_url: str
    certified_at: str
    hotfix_of: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class ChannelPointer:
    channel: Channel
    version: str
    sha: str
    updated_at: str
    reason: str = ""


@dataclass(frozen=True)
class TargetRequest:
    target_mode: TargetMode
    channel: Channel | None = None
    release_tag: str | None = None
    branch: str | None = None
    commit_rev: str | None = None


@dataclass(frozen=True)
class TargetSnapshot:
    repo_display: str
    peeled_sha: str | None = None
    tag_body: str | None = None
    candidate_tags: Mapping[str, str | None] = field(default_factory=dict[str, str | None])
    branch_tip_sha: str | None = None
    pinned_sha: str | None = None
    pinned_on_branch: bool = False


@dataclass(frozen=True)
class ResolvedTarget:
    sha: str
    notice: str
    certificate: ReleaseCertificate | None = None
    unreleased_branch: str | None = None


def parse_release_tag(raw: str) -> ReleaseVersion | None:
    match = _CALVER_TAG.match(raw)
    if match is None:
        return None
    year, month, day = int(match[1]), int(match[2]), int(match[3])
    hour, minute, second = int(match[4]), int(match[5]), int(match[6])
    hotfix = int(match[7]) if match[7] is not None else 0
    try:
        datetime(year, month, day, hour, minute, second, tzinfo=UTC)
    except ValueError:
        return None
    return ReleaseVersion(year, month, day, hour, minute, second, hotfix)


def _next_hotfix_ordinal(base_tag: str, existing_tags: Iterable[str]) -> tuple[str, int]:
    parsed_base = parse_release_tag(base_tag)
    if parsed_base is None or parsed_base.hotfix != 0:
        raise ValueError(
            f"hotfix base {base_tag!r} must be a non-hotfix CalVer release tag (vYYYY.MM.DD.HHMMSS)"
        )
    highest = 0
    for tag in existing_tags:
        parsed = parse_release_tag(tag)
        if parsed is not None and parsed.base_tag == parsed_base.base_tag:
            highest = max(highest, parsed.hotfix)
    return parsed_base.base_tag, highest + 1


def mint_release_tag(
    certified_at: datetime,
    *,
    hotfix: int | None = None,
    hotfix_of: str | None = None,
    existing_tags: Iterable[str] = (),
) -> str:
    if certified_at.tzinfo is None or certified_at.utcoffset() is None:
        raise ValueError("certified_at must carry an explicit timezone")
    if hotfix_of is not None:
        base_tag, ordinal = _next_hotfix_ordinal(hotfix_of, existing_tags)
        return f"{base_tag}-hf{ordinal}"
    if hotfix is not None and hotfix < 1:
        raise ValueError(f"hotfix ordinal must be >= 1, got {hotfix}")
    utc = certified_at.astimezone(UTC)
    version = ReleaseVersion(
        utc.year,
        utc.month,
        utc.day,
        utc.hour,
        utc.minute,
        utc.second,
        hotfix or 0,
    )
    return version.tag


def _is_object_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _is_str_dict(value: object) -> TypeGuard[dict[str, object]]:
    return _is_object_dict(value) and all(isinstance(k, str) for k in value)


def _extract_delimited_json(
    body: str, begin_marker: str, end_marker: str
) -> dict[str, object] | None:
    _before_begin, found_begin, rest = body.partition(begin_marker)
    if not found_begin or begin_marker in rest:
        return None
    json_block, found_end, after_end = rest.partition(end_marker)
    if not found_end or end_marker in after_end:
        return None
    try:
        parsed: object = json.loads(json_block.strip())
    except json.JSONDecodeError:
        return None
    return parsed if _is_str_dict(parsed) else None


def format_tag_message(record: ReleaseCertificate | ChannelPointer) -> str:
    if isinstance(record, ReleaseCertificate):
        cert_payload: dict[str, str | None] = {
            "schema": CERTIFICATE_SCHEMA,
            "version": record.version,
            "sha": record.sha,
            "source_ref": record.source_ref,
            "gate_run_id": record.gate_run_id,
            "gate_run_url": record.gate_run_url,
            "certified_at": record.certified_at,
            "hotfix_of": record.hotfix_of,
            "reason": record.reason,
        }
        encoded = json.dumps(cert_payload, indent=2, sort_keys=True)
        return (
            f"{record.version} certified by the release gate\n\n"
            f"{BEGIN_CERTIFICATE}\n{encoded}\n{END_CERTIFICATE}\n"
        )
    chan_payload: dict[str, str] = {
        "schema": CHANNEL_SCHEMA,
        "channel": record.channel,
        "version": record.version,
        "sha": record.sha,
        "updated_at": record.updated_at,
        "reason": record.reason,
    }
    encoded = json.dumps(chan_payload, indent=2, sort_keys=True)
    return (
        f"channel/{record.channel} -> {record.version} ({record.sha[:12]})\n\n"
        f"{BEGIN_CHANNEL}\n{encoded}\n{END_CHANNEL}\n"
    )


def _non_empty_str(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _validate_hotfix_lineage(
    tag_name: str, parsed_version: ReleaseVersion, raw_hotfix: object
) -> tuple[str | None, str | None]:
    if raw_hotfix is None:
        if parsed_version.hotfix > 0:
            return None, f"hotfix tag {tag_name} certificate must record hotfix_of"
        return None, None
    if not isinstance(raw_hotfix, str) or parse_release_tag(raw_hotfix) is None:
        return None, f"tag {tag_name} certificate has invalid hotfix_of {raw_hotfix!r}"
    return raw_hotfix, None


def _decode_certificate_fields(
    tag_name: str,
    peeled_commit_sha: str,
    parsed_version: ReleaseVersion,
    payload: Mapping[str, object],
) -> tuple[ReleaseCertificate | None, str]:
    version = _non_empty_str(payload, "version")
    sha = _non_empty_str(payload, "sha")
    source_ref = _non_empty_str(payload, "source_ref")
    gate_run_id = _non_empty_str(payload, "gate_run_id")
    gate_run_url = _non_empty_str(payload, "gate_run_url")
    certified_at = _non_empty_str(payload, "certified_at")
    if (
        version is None
        or sha is None
        or source_ref is None
        or gate_run_id is None
        or gate_run_url is None
        or certified_at is None
    ):
        return None, f"tag {tag_name} certificate is missing required fields"
    if version != tag_name:
        return None, f"tag {tag_name} certificate names version {version!r}, not {tag_name!r}"
    if sha != peeled_commit_sha:
        return (
            None,
            f"tag {tag_name} certificate names commit {sha[:12]}, "
            f"not peeled commit {peeled_commit_sha[:12]}",
        )
    hotfix_of, hotfix_err = _validate_hotfix_lineage(
        tag_name, parsed_version, payload.get("hotfix_of")
    )
    if hotfix_err is not None:
        return None, hotfix_err
    raw_reason = payload.get("reason", "")
    if not isinstance(raw_reason, str):
        return None, f"tag {tag_name} certificate reason must be a string"
    return (
        ReleaseCertificate(
            version=version,
            sha=sha,
            source_ref=source_ref,
            gate_run_id=gate_run_id,
            gate_run_url=gate_run_url,
            certified_at=certified_at,
            hotfix_of=hotfix_of,
            reason=raw_reason,
        ),
        "verified",
    )


# meiva:note
# Verify both the tag's CalVer name and its embedded `meiva-release/v1` JSON certificate against
# the peeled commit SHA so neither a handwritten tag nor a certificate copied onto another tag or
# commit can manufacture release eligibility.
def parse_tag_certificate(
    tag_name: str, peeled_commit_sha: str, tag_body: str | None
) -> tuple[ReleaseCertificate | None, str]:
    parsed_version = parse_release_tag(tag_name)
    if parsed_version is None:
        return (
            None,
            f"tag {tag_name!r} is not a CalVer release tag (vYYYY.MM.DD.HHMMSS[-hfN])",
        )
    if _FULL_SHA.match(peeled_commit_sha) is None:
        return None, f"tag {tag_name} does not peel to a 40-hex commit SHA"
    if tag_body is None:
        return (
            None,
            f"tag {tag_name} is not an annotated tag carrying a {CERTIFICATE_SCHEMA} certificate",
        )
    payload = _extract_delimited_json(tag_body, BEGIN_CERTIFICATE, END_CERTIFICATE)
    if payload is None:
        return None, f"tag {tag_name} lacks a valid {CERTIFICATE_SCHEMA} JSON certificate"
    if payload.get("schema") != CERTIFICATE_SCHEMA:
        return (
            None,
            f"tag {tag_name} certificate schema must be {CERTIFICATE_SCHEMA!r}, "
            f"got {payload.get('schema')!r}",
        )
    return _decode_certificate_fields(tag_name, peeled_commit_sha, parsed_version, payload)


def _decode_channel_fields(
    channel: Channel, tag_ref: str, peeled_commit_sha: str, payload: Mapping[str, object]
) -> tuple[ChannelPointer | None, str]:
    version = _non_empty_str(payload, "version")
    sha = _non_empty_str(payload, "sha")
    updated_at = _non_empty_str(payload, "updated_at")
    if version is None or sha is None or updated_at is None:
        return None, f"{tag_ref} pointer is missing required fields"
    if parse_release_tag(version) is None:
        return None, f"{tag_ref} pointer version {version!r} is not a CalVer release tag"
    if sha != peeled_commit_sha:
        return (
            None,
            f"{tag_ref} pointer names commit {sha[:12]}, "
            f"not peeled commit {peeled_commit_sha[:12]}",
        )
    raw_reason = payload.get("reason", "")
    if not isinstance(raw_reason, str):
        return None, f"{tag_ref} pointer reason must be a string"
    return (
        ChannelPointer(
            channel=channel,
            version=version,
            sha=sha,
            updated_at=updated_at,
            reason=raw_reason,
        ),
        "verified",
    )


def parse_channel_pointer(
    channel: Channel, peeled_commit_sha: str, tag_body: str | None
) -> tuple[ChannelPointer | None, str]:
    tag_ref = f"channel/{channel}"
    if _FULL_SHA.match(peeled_commit_sha) is None:
        return None, f"{tag_ref} does not peel to a 40-hex commit SHA"
    if tag_body is None:
        return None, f"{tag_ref} is not an annotated channel tag"
    payload = _extract_delimited_json(tag_body, BEGIN_CHANNEL, END_CHANNEL)
    if payload is None:
        return None, f"{tag_ref} lacks a valid {CHANNEL_SCHEMA} JSON pointer"
    if payload.get("schema") != CHANNEL_SCHEMA:
        return (
            None,
            f"{tag_ref} pointer schema must be {CHANNEL_SCHEMA!r}, got {payload.get('schema')!r}",
        )
    if payload.get("channel") != channel:
        return (
            None,
            f"{tag_ref} pointer names channel {payload.get('channel')!r}, not {channel!r}",
        )
    return _decode_channel_fields(channel, tag_ref, peeled_commit_sha, payload)


def _highest_certified_on_commit(
    commit_sha: str, candidate_tags: Mapping[str, str | None]
) -> tuple[ReleaseCertificate | None, str]:
    verified: list[tuple[ReleaseVersion, ReleaseCertificate]] = []
    calver_errors: list[str] = []
    for tag_name in sorted(candidate_tags):
        parsed = parse_release_tag(tag_name)
        if parsed is None:
            continue
        cert, reason = parse_tag_certificate(tag_name, commit_sha, candidate_tags[tag_name])
        if cert is not None:
            verified.append((parsed, cert))
        else:
            calver_errors.append(reason)
    if verified:
        _ver, best = max(verified, key=lambda pair: pair[0])
        return best, "verified"
    if calver_errors:
        return None, "; ".join(calver_errors)
    return None, f"commit {commit_sha[:12]} carries no {CERTIFICATE_SCHEMA} release tag"


def _resolve_channel_mode(
    channel: Channel | None, snapshot: TargetSnapshot
) -> tuple[ResolvedTarget | None, str]:
    if channel is None:
        return None, "--channel value is missing"
    tag_ref = f"channel/{channel}"
    if snapshot.peeled_sha is None:
        return (
            None,
            f"refused: {tag_ref} tag is not present in {snapshot.repo_display}; "
            "run `git fetch --tags --force origin` (or certify/promote a release first)",
        )
    pointer, reason = parse_channel_pointer(channel, snapshot.peeled_sha, snapshot.tag_body)
    if pointer is None:
        return None, f"refused: {reason}"
    if pointer.version not in snapshot.candidate_tags:
        return (
            None,
            f"refused: {tag_ref} points to {pointer.version}, which is missing in "
            f"{snapshot.repo_display}; run `git fetch --tags --force origin`",
        )
    cert, cert_reason = parse_tag_certificate(
        pointer.version, snapshot.peeled_sha, snapshot.candidate_tags[pointer.version]
    )
    if cert is None:
        return None, f"refused: {tag_ref} target {pointer.version} is uncertified ({cert_reason})"
    notice = (
        f"target: channel {channel} -> {cert.version} @ {snapshot.peeled_sha[:12]} "
        f"(certified: {cert.gate_run_url})"
    )
    return ResolvedTarget(sha=snapshot.peeled_sha, notice=notice, certificate=cert), "verified"


def _resolve_release_mode(
    release_tag: str | None, snapshot: TargetSnapshot
) -> tuple[ResolvedTarget | None, str]:
    if release_tag is None:
        return None, "--release value is missing"
    if parse_release_tag(release_tag) is None:
        return (
            None,
            f"refused: {release_tag!r} is not a CalVer release tag (vYYYY.MM.DD.HHMMSS[-hfN])",
        )
    if snapshot.peeled_sha is None:
        return (
            None,
            f"refused: release tag {release_tag} is not present in {snapshot.repo_display}; "
            "run `git fetch --tags --force origin`",
        )
    cert, reason = parse_tag_certificate(release_tag, snapshot.peeled_sha, snapshot.tag_body)
    if cert is None:
        return None, f"refused: {reason}"
    notice = (
        f"target: release {cert.version} @ {snapshot.peeled_sha[:12]} "
        f"(certified: {cert.gate_run_url})"
    )
    return ResolvedTarget(sha=snapshot.peeled_sha, notice=notice, certificate=cert), "verified"


def _resolve_commit_mode(
    commit_rev: str | None, snapshot: TargetSnapshot
) -> tuple[ResolvedTarget | None, str]:
    if commit_rev is None:
        return None, "--new value is missing"
    if snapshot.peeled_sha is None:
        return (
            None,
            f"refused: {commit_rev} is not a commit in {snapshot.repo_display}; git fetch origin",
        )
    if parse_release_tag(commit_rev) is not None and commit_rev in snapshot.candidate_tags:
        cert, reason = parse_tag_certificate(
            commit_rev, snapshot.peeled_sha, snapshot.candidate_tags[commit_rev]
        )
        if cert is None:
            return None, f"refused: {reason}"
    else:
        cert, reason = _highest_certified_on_commit(snapshot.peeled_sha, snapshot.candidate_tags)
        if cert is None:
            return (
                None,
                f"refused: {reason}; pass --channel stable|latest, --release <tag>, "
                "or --track-branch <branch> for unreleased branch commits",
            )
    notice = (
        f"target: release {cert.version} @ {snapshot.peeled_sha[:12]} "
        f"(certified: {cert.gate_run_url})"
    )
    return ResolvedTarget(sha=snapshot.peeled_sha, notice=notice, certificate=cert), "verified"


def _resolve_branch_mode(
    branch: str | None, pinned_rev: str | None, snapshot: TargetSnapshot
) -> tuple[ResolvedTarget | None, str]:
    if branch is None:
        return None, "--track-branch value is missing"
    if snapshot.branch_tip_sha is None:
        return (
            None,
            f"refused: origin/{branch} is not present in {snapshot.repo_display}; "
            f"push the branch and run `git fetch origin {branch}`",
        )
    if pinned_rev is None:
        target_sha = snapshot.branch_tip_sha
    else:
        if snapshot.pinned_sha is None:
            return (
                None,
                f"refused: {pinned_rev} is not a commit in {snapshot.repo_display}; git fetch origin",
            )
        if not snapshot.pinned_on_branch:
            return (
                None,
                f"refused: commit {snapshot.pinned_sha[:12]} ({pinned_rev}) is not an ancestor of "
                f"origin/{branch} ({snapshot.branch_tip_sha[:12]})",
            )
        target_sha = snapshot.pinned_sha
    notice = f"target: UNRELEASED branch {branch} @ {target_sha} (preview / rapid-iteration mode)"
    return (
        ResolvedTarget(sha=target_sha, notice=notice, unreleased_branch=branch),
        "unreleased-branch",
    )


def resolve_update_target(
    request: TargetRequest, snapshot: TargetSnapshot
) -> tuple[ResolvedTarget | None, str]:
    match request.target_mode:
        case "channel":
            return _resolve_channel_mode(request.channel, snapshot)
        case "release":
            return _resolve_release_mode(request.release_tag, snapshot)
        case "commit":
            return _resolve_commit_mode(request.commit_rev, snapshot)
        case "track-branch":
            return _resolve_branch_mode(request.branch, request.commit_rev, snapshot)
