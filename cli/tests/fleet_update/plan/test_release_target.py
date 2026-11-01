# meiva:note
# Pure unit tests for CalVer tag parsing/minting, tamper-evident `meiva-release/v1` tag
# certificates, `meiva-channel/v1` channel pointers, and the three fleet target-resolution modes.
# meiva:link docs/conventions/engineering/host-versioning.md

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from cli.fleet_update.plan.release_target import (
    ChannelPointer,
    ReleaseCertificate,
    TargetRequest,
    TargetSnapshot,
    format_tag_message,
    mint_release_tag,
    parse_channel_pointer,
    parse_release_tag,
    parse_tag_certificate,
    resolve_update_target,
)

_SHA_A = "a" * 40
_SHA_B = "b" * 40
_RUN_URL = "https://github.com/zhiyuan-zhang0206/Meiva/actions/runs/42"


def _cert(
    version: str = "v2026.11.01.143022",
    sha: str = _SHA_A,
    hotfix_of: str | None = None,
) -> ReleaseCertificate:
    return ReleaseCertificate(
        version=version,
        sha=sha,
        source_ref="refs/heads/main",
        gate_run_id="42",
        gate_run_url=_RUN_URL,
        certified_at="2026-11-01T14:30:22Z",
        hotfix_of=hotfix_of,
    )


def test_calver_tags_parse_validate_calendar_and_order_hotfixes() -> None:
    base = parse_release_tag("v2026.11.01.143022")
    hf1 = parse_release_tag("v2026.11.01.143022-hf1")
    hf2 = parse_release_tag("v2026.11.01.143022-hf2")
    later = parse_release_tag("v2026.11.01.143023")
    assert base is not None and hf1 is not None and hf2 is not None and later is not None
    assert base < hf1 < hf2 < later
    assert base.tag == "v2026.11.01.143022"
    assert hf1.base_tag == "v2026.11.01.143022"
    assert hf1.tag == "v2026.11.01.143022-hf1"

    for invalid in (
        "2026.11.01.143022",
        "v1.0.0",
        "v2026.11.01",
        "v2026.02.30.120000",
        "v2026.11.01.250000",
        "v2026.11.01.143022-hf0",
        "v2026.11.01.143022-hf01",
        "v2026.11.01.143022-rc1",
    ):
        assert parse_release_tag(invalid) is None, invalid


def test_mint_release_tag_normalizes_to_utc_and_increments_hotfix_lineage() -> None:
    tz_plus8 = timezone(timedelta(hours=8))
    stamped = datetime(2026, 11, 1, 22, 30, 22, tzinfo=tz_plus8)
    assert mint_release_tag(stamped) == "v2026.11.01.143022"
    assert mint_release_tag(stamped, hotfix=2) == "v2026.11.01.143022-hf2"
    with pytest.raises(ValueError, match="timezone"):
        mint_release_tag(datetime(2026, 11, 1, 14, 30, 22))  # noqa: DTZ001
    with pytest.raises(ValueError, match="hotfix ordinal"):
        mint_release_tag(datetime(2026, 11, 1, 14, 30, 22, tzinfo=UTC), hotfix=0)

    assert mint_release_tag(stamped, hotfix_of="v2026.11.01.143022") == "v2026.11.01.143022-hf1"
    existing = [
        "v2026.11.01.143022",
        "v2026.11.01.143022-hf1",
        "v2026.11.01.143022-hf3",
        "v2026.11.02.000000-hf9",
    ]
    assert (
        mint_release_tag(stamped, hotfix_of="v2026.11.01.143022", existing_tags=existing)
        == "v2026.11.01.143022-hf4"
    )
    with pytest.raises(ValueError, match="non-hotfix CalVer"):
        mint_release_tag(stamped, hotfix_of="v2026.11.01.143022-hf1", existing_tags=existing)


def test_certificate_round_trips_and_detects_tampering() -> None:
    cert = _cert()
    body = format_tag_message(cert)
    parsed, reason = parse_tag_certificate("v2026.11.01.143022", _SHA_A, body)
    assert parsed == cert and reason == "verified"

    wrong_sha, err = parse_tag_certificate("v2026.11.01.143022", _SHA_B, body)
    assert wrong_sha is None and "not peeled commit" in err

    wrong_tag, err = parse_tag_certificate("v2026.11.01.143023", _SHA_A, body)
    assert wrong_tag is None and "names version" in err

    missing_hf_base = format_tag_message(_cert(version="v2026.11.01.143022-hf1"))
    bad_hf, err = parse_tag_certificate("v2026.11.01.143022-hf1", _SHA_A, missing_hf_base)
    assert bad_hf is None and "must record hotfix_of" in err

    good_hf_cert = _cert(
        version="v2026.11.01.143022-hf1",
        hotfix_of="v2026.11.01.143022",
    )
    good_hf, reason = parse_tag_certificate(
        "v2026.11.01.143022-hf1",
        _SHA_A,
        format_tag_message(good_hf_cert),
    )
    assert good_hf == good_hf_cert and reason == "verified"


def test_channel_pointer_requires_matching_channel_and_certified_release_tag() -> None:
    cert = _cert()
    cert_body = format_tag_message(cert)
    pointer = ChannelPointer(
        channel="stable",
        version=cert.version,
        sha=_SHA_A,
        updated_at="2026-11-01T15:00:00Z",
        reason="promote after soak",
    )
    ptr_body = format_tag_message(pointer)
    parsed_ptr, reason = parse_channel_pointer("stable", _SHA_A, ptr_body)
    assert parsed_ptr == pointer and reason == "verified"

    wrong_ch, err = parse_channel_pointer("latest", _SHA_A, ptr_body)
    assert wrong_ch is None and "names channel 'stable'" in err

    resolved, status = resolve_update_target(
        TargetRequest(target_mode="channel", channel="stable"),
        TargetSnapshot(
            repo_display="/repo",
            peeled_sha=_SHA_A,
            tag_body=ptr_body,
            candidate_tags={cert.version: cert_body},
        ),
    )
    assert resolved is not None and status == "verified"
    assert resolved.sha == _SHA_A and "channel stable -> v2026.11.01.143022" in resolved.notice

    dangling, err = resolve_update_target(
        TargetRequest(target_mode="channel", channel="stable"),
        TargetSnapshot(
            repo_display="/repo",
            peeled_sha=_SHA_A,
            tag_body=ptr_body,
            candidate_tags={},
        ),
    )
    assert dangling is None and "is missing in /repo" in err


def test_commit_and_release_and_branch_resolution_modes() -> None:
    c1 = _cert("v2026.11.01.120000", _SHA_A)
    c2 = _cert("v2026.11.01.143022", _SHA_A)
    tags: dict[str, str | None] = {
        "m2-full": None,
        c1.version: format_tag_message(c1),
        c2.version: format_tag_message(c2),
    }

    by_sha, _ = resolve_update_target(
        TargetRequest(target_mode="commit", commit_rev=_SHA_A[:12]),
        TargetSnapshot(repo_display="/repo", peeled_sha=_SHA_A, candidate_tags=tags),
    )
    assert by_sha is not None and by_sha.certificate == c2

    by_rel, _ = resolve_update_target(
        TargetRequest(target_mode="release", release_tag=c1.version),
        TargetSnapshot(repo_display="/repo", peeled_sha=_SHA_A, tag_body=tags[c1.version]),
    )
    assert by_rel is not None and by_rel.certificate == c1

    by_branch, mode = resolve_update_target(
        TargetRequest(target_mode="track-branch", branch="feat/x"),
        TargetSnapshot(repo_display="/repo", branch_tip_sha=_SHA_B),
    )
    assert by_branch is not None and mode == "unreleased-branch"
    assert by_branch.sha == _SHA_B
    assert by_branch.notice == (
        f"target: UNRELEASED branch feat/x @ {_SHA_B} (preview / rapid-iteration mode)"
    )
