from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from services.backup.artifact import offsite_plan
from services.backup.artifact.tests.oss_fake import md5_hex

_METADATA = {"meiva-artifact-kind": "logical-backup"}
_ETAGS = ["AAAA", "BBBB"]
_REPORTED_METADATA = {"x-oss-meta-meiva-artifact-kind": "logical-backup"}
_DIFFERS = "immutable OSS object differs from the local artifact"


def test_unset_keys_names_exactly_the_missing_fields_in_operator_order() -> None:
    assert offsite_plan.unset_keys("", "", None) == (
        "MEIVA_BACKUP_OFFSITE_ENDPOINT",
        "MEIVA_BACKUP_OFFSITE_BUCKET",
        "MEIVA_BACKUP_OFFSITE_CREDENTIALS_FILE",
    )
    assert offsite_plan.unset_keys("https://oss", "", None) == (
        "MEIVA_BACKUP_OFFSITE_BUCKET",
        "MEIVA_BACKUP_OFFSITE_CREDENTIALS_FILE",
    )
    assert offsite_plan.unset_keys("https://oss", "b", Path("/oss.json")) == ()


def test_the_part_header_is_the_base64_of_the_binary_digest() -> None:
    assert offsite_plan.part_md5_header(md5_hex(b"part")) == {
        "Content-MD5": "9Mk4XxkC9zNLALm07NFk3g=="
    }


def test_the_chain_etag_is_the_md5_of_the_joined_part_etags_and_the_part_count() -> None:
    assert offsite_plan.chain_etag(_ETAGS) == f"{md5_hex(b'AAAABBBB')}-2"
    assert offsite_plan.chain_etag(_ETAGS[:1]).endswith("-1")


def test_etags_lose_their_http_quotes_and_keep_their_case() -> None:
    assert offsite_plan.normalize_etag('"AbC-2"') == "AbC-2"
    assert offsite_plan.normalize_etag("  plain ") == "plain"
    assert offsite_plan.normalize_etag(None) == ""


def test_metadata_travels_as_prefixed_headers() -> None:
    assert offsite_plan.metadata_headers(_METADATA) == _REPORTED_METADATA


def _problem(
    *,
    etag: str | None = f'"{offsite_plan.chain_etag(_ETAGS).upper()}"',
    content_length: int | None = 10,
    headers: Mapping[str, str] = _REPORTED_METADATA,
    part_etags: Sequence[str] = _ETAGS,
) -> str | None:
    return offsite_plan.object_problem(
        etag=etag,
        content_length=content_length,
        headers=headers,
        size=10,
        part_etags=part_etags,
        metadata=_METADATA,
    )


def test_the_object_the_store_reports_matches_what_was_uploaded() -> None:
    assert _problem() is None


def test_a_missing_property_is_its_own_problem() -> None:
    omitted = "OSS object omitted its verification properties"

    assert _problem(etag=None) == omitted
    assert _problem(etag="") == omitted
    assert _problem(content_length=None) == omitted


def test_another_size_chain_or_metadata_is_a_difference() -> None:
    assert _problem(content_length=11) == _DIFFERS
    assert _problem(etag='"0123-2"') == _DIFFERS
    assert _problem(part_etags=["AAAA"]) == _DIFFERS
    assert _problem(headers={"x-oss-meta-meiva-artifact-kind": "other"}) == _DIFFERS
    assert _problem(headers={}) == _DIFFERS
