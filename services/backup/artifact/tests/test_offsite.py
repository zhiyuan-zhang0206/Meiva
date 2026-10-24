from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

import oss2
import pytest

from services.backup.artifact import offsite
from services.backup.artifact.names import REMOTE_ROOT
from services.backup.artifact.offsite_plan import OssTarget
from services.backup.artifact.tests.oss_fake import (
    BUCKET,
    FakeOss,
    chain_etag,
    fake_oss,
    md5_hex,
)

_NAME = "meiva-20260916T030000Z.dump.enc"
_METADATA = {"meiva-artifact-kind": "logical-backup"}
_CREDENTIALS = '{"access_key_id": "id", "access_key_secret": "secret"}'


def _artifact(tmp_path: Path, data: bytes = b"encrypted artifact") -> Path:
    path = tmp_path / _NAME
    path.write_bytes(data)
    return path


def _target(server: FakeOss, tmp_path: Path) -> OssTarget:
    credentials = tmp_path / "oss.json"
    credentials.write_text(_CREDENTIALS)
    return OssTarget(server.endpoint, BUCKET, credentials)


def _bucket(server: FakeOss, tmp_path: Path) -> oss2.Bucket:
    return offsite.open_bucket(_target(server, tmp_path))


def test_publish_streams_a_multipart_object_and_verifies_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    artifact = _artifact(tmp_path)
    caplog.set_level(logging.INFO, logger=offsite.__name__)

    with fake_oss() as server:
        published = offsite.publish(artifact, _target(server, tmp_path))

    assert published == f"{REMOTE_ROOT}/{_NAME}"
    stored = server.files[f"{REMOTE_ROOT}/{_NAME}"]
    assert stored["data"] == b"encrypted artifact"
    assert stored["headers"] == {"x-oss-meta-meiva-artifact-kind": "logical-backup"}
    assert stored["etag"] == chain_etag([b"encrypted artifact"])
    assert server.aborted == []
    [ack_line] = [r.getMessage() for r in caplog.records if "off-site published" in r.getMessage()]
    assert f"{published} (size={len(b'encrypted artifact')}" in ack_line
    assert f"pin={stored['etag']}" in ack_line
    assert f"checksum=md5:{md5_hex(b'encrypted artifact')}" in ack_line
    assert artifact.read_bytes() == b"encrypted artifact"


def test_forbid_overwrite_rides_only_on_complete(tmp_path: Path) -> None:
    with fake_oss() as server:
        offsite.publish(_artifact(tmp_path), _target(server, tmp_path))

    headers = dict(server.calls)
    assert "x-oss-forbid-overwrite" not in headers["init"]
    assert "x-oss-forbid-overwrite" not in headers["part"]
    assert headers["complete"] == {"x-oss-forbid-overwrite": "true"}
    assert headers["part"]["content-md5"]


def test_a_large_artifact_is_split_into_chained_parts(tmp_path: Path) -> None:
    payload = b"0123456789abcdefg"

    with fake_oss() as server:
        ack = offsite.put_if_absent(
            _bucket(server, tmp_path),
            _artifact(tmp_path, payload),
            "x/object",
            _METADATA,
            part_size=5,
        )

    assert ack.created and ack.size == len(payload)
    assert ack.pin_token == chain_etag([payload[0:5], payload[5:10], payload[10:15], payload[15:]])
    assert ack.pin_token.endswith("-4")
    assert ack.md5 == md5_hex(payload)
    assert server.files["x/object"]["data"] == payload


def test_adopt_after_crash_returns_the_existing_object(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    artifact = _artifact(tmp_path)
    caplog.set_level(logging.INFO, logger=offsite.__name__)

    with fake_oss() as server:
        bucket = _bucket(server, tmp_path)
        first = offsite.put_if_absent(bucket, artifact, f"{REMOTE_ROOT}/{_NAME}", _METADATA)
        second = offsite.put_if_absent(bucket, artifact, f"{REMOTE_ROOT}/{_NAME}", _METADATA)
        republished = offsite.publish(artifact, _target(server, tmp_path))

    assert first.created and not second.created
    assert second.pin_token == first.pin_token and second.md5 == first.md5
    assert server.aborted[0] == "up2", "the losing upload must not linger as a fragment"
    assert republished == f"{REMOTE_ROOT}/{_NAME}"
    assert any("off-site published" in r.getMessage() for r in caplog.records)


def test_an_occupied_name_with_a_different_etag_chain_is_not_adopted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    artifact = _artifact(tmp_path)

    with fake_oss() as server:
        server.seed(f"{REMOTE_ROOT}/{_NAME}", [b"eNcrypted artifact"], _METADATA)
        with pytest.raises(offsite.OffsiteError, match="differs from the local artifact"):
            offsite.put_if_absent(
                _bucket(server, tmp_path), artifact, f"{REMOTE_ROOT}/{_NAME}", _METADATA
            )
        assert server.files[f"{REMOTE_ROOT}/{_NAME}"]["data"] == b"eNcrypted artifact"
        caplog.set_level(logging.INFO, logger=offsite.__name__)
        assert offsite.publish(artifact, _target(server, tmp_path)) is None

    assert any(
        "off-site publish of" in r.getMessage() and "failed" in r.getMessage()
        for r in caplog.records
    )
    assert artifact.read_bytes() == b"encrypted artifact"


def test_an_occupied_name_with_different_metadata_is_not_adopted(tmp_path: Path) -> None:
    with fake_oss() as server:
        server.seed(
            f"{REMOTE_ROOT}/{_NAME}", [b"encrypted artifact"], {"meiva-artifact-kind": "other"}
        )
        with pytest.raises(offsite.OffsiteError, match="differs from the local artifact"):
            offsite.put_if_absent(
                _bucket(server, tmp_path), _artifact(tmp_path), f"{REMOTE_ROOT}/{_NAME}", _METADATA
            )


def test_a_part_etag_that_is_not_its_content_md5_aborts_the_upload(tmp_path: Path) -> None:
    with fake_oss() as server:
        server.corrupt_part_etags = True
        with pytest.raises(offsite.OffsiteError, match="part ETag does not match"):
            offsite.put_if_absent(
                _bucket(server, tmp_path), _artifact(tmp_path), "x/object", _METADATA
            )

    assert "x/object" not in server.files
    assert server.aborted == ["up1"]


def test_a_completed_etag_that_breaks_the_part_chain_is_refused(tmp_path: Path) -> None:
    with fake_oss() as server:
        server.corrupt_complete_etag = True
        with pytest.raises(offsite.OffsiteError, match="differs from the local artifact"):
            offsite.put_if_absent(
                _bucket(server, tmp_path), _artifact(tmp_path), "x/object", _METADATA
            )


def test_an_empty_artifact_is_refused_before_any_request(tmp_path: Path) -> None:
    with fake_oss() as server, pytest.raises(offsite.OffsiteError, match="non-empty"):
        offsite.put_if_absent(
            _bucket(server, tmp_path), _artifact(tmp_path, b""), "x/object", _METADATA
        )

    assert server.calls == []


def test_a_rejected_init_keeps_the_local_artifact_and_does_not_raise(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    artifact = _artifact(tmp_path)

    with fake_oss() as server:
        server.fail_init = (403, "AccessDenied")
        assert offsite.publish(artifact, _target(server, tmp_path)) is None

    assert artifact.read_bytes() == b"encrypted artifact"
    assert any(r.exc_info is not None for r in caplog.records), "the failure must carry its cause"


def test_the_root_is_a_parameter(tmp_path: Path) -> None:
    with fake_oss() as server:
        published = offsite.publish(
            _artifact(tmp_path), _target(server, tmp_path), root="meiva-pitr-scratch/t1"
        )

    assert published == f"meiva-pitr-scratch/t1/{_NAME}"
    assert list(server.files) == [published]


def test_an_unconfigured_destination_skips_with_one_info_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=offsite.__name__)

    assert offsite.configured_target("", "", None) is None

    [record] = caplog.records
    assert (record.levelno, record.exc_info) == (logging.INFO, None)
    for key in ("ENDPOINT", "BUCKET", "CREDENTIALS_FILE"):
        assert f"MEIVA_BACKUP_OFFSITE_{key}" in record.getMessage()


def test_publishing_to_no_destination_publishes_nothing(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)

    assert offsite.publish(artifact, None) is None

    assert artifact.read_bytes() == b"encrypted artifact"


def test_a_partly_configured_destination_skips_naming_the_unset_keys(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=offsite.__name__)

    assert offsite.configured_target("https://oss-cn-test.example.com/", "", None) is None

    [record] = caplog.records
    assert record.levelno == logging.INFO
    assert "MEIVA_BACKUP_OFFSITE_BUCKET" in record.getMessage()
    assert "MEIVA_BACKUP_OFFSITE_CREDENTIALS_FILE" in record.getMessage()
    assert "MEIVA_BACKUP_OFFSITE_ENDPOINT" not in record.getMessage()


def test_a_complete_destination_is_one_target_value(tmp_path: Path) -> None:
    assert offsite.configured_target(
        "https://oss-cn-test.example.com/", "backups", tmp_path / "oss.json"
    ) == OssTarget("https://oss-cn-test.example.com/", "backups", tmp_path / "oss.json")


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b'["a"]',
        b'{"access_key_id": "id"}',
        b'{"access_key_id": "", "access_key_secret": "s"}',
    ],
)
def test_an_unusable_credentials_file_keeps_the_local_artifact(
    payload: bytes, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    credentials = tmp_path / "oss.json"
    credentials.write_bytes(payload)
    artifact = _artifact(tmp_path)

    assert offsite.publish(artifact, OssTarget("http://127.0.0.1:1", BUCKET, credentials)) is None

    assert artifact.read_bytes() == b"encrypted artifact"
    assert any(
        "off-site store unavailable" in r.getMessage() and r.exc_info is not None
        for r in caplog.records
    )


def test_open_bucket_reads_the_access_key_pair(tmp_path: Path) -> None:
    credentials = tmp_path / "oss.json"
    credentials.write_text(_CREDENTIALS)

    bucket = cast(
        Any,
        offsite.open_bucket(OssTarget("https://oss-cn-test.example.com/", "backups", credentials)),
    )

    assert bucket.bucket_name == "backups"
    assert bucket.endpoint == "https://oss-cn-test.example.com"
