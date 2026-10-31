from __future__ import annotations

import base64
import dataclasses
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from meiva.self.harness.registration import (
    HarnessRegistration,
    RegistrationError,
    decode_registration,
    handle_digest,
)
from meiva.self.harness.store import FileRegistrationStore, registration_store


def registration(handle: str) -> HarnessRegistration:
    return HarnessRegistration(
        agent_id=41,
        harness="deepseek",
        external_session_id="session-1",
        machine="ubuntu",
        controller_pid=os.getpid(),
        handle_sha256=handle_digest(handle),
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )


def test_store_round_trips_a_registration(tmp_path: Path) -> None:
    store = FileRegistrationStore(tmp_path)
    record = registration(_handle())
    store.save(record)
    assert store.load(record.agent_id) == record


# meiva:note
# The store of a home keeps its rows under `<home>/harness/registrations`, wherever the process
# environment points: a registration saved through one home is not visible through another.
def test_the_registration_store_of_a_home_keeps_its_rows_under_that_home(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    record = registration(_handle())
    registration_store(first).save(record)

    assert any((first / "harness" / "registrations").glob("*.json"))
    assert registration_store(first).load(record.agent_id) == record
    assert registration_store(second).load(record.agent_id) is None


def test_store_finds_a_registration_by_its_handle(tmp_path: Path) -> None:
    store = FileRegistrationStore(tmp_path)
    handle = _handle()
    record = registration(handle)
    store.save(record)
    assert store.find_by_handle(handle) == record
    assert store.find_by_handle(_handle()) is None


def test_a_registration_records_the_digest_not_the_handle(tmp_path: Path) -> None:
    store = FileRegistrationStore(tmp_path)
    handle = _handle()
    store.save(registration(handle))
    text = store.path(41).read_text(encoding="utf-8")
    assert handle not in text
    assert handle_digest(handle) in text


def test_expiry_is_read_from_an_iso_timestamp() -> None:
    now = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
    past = (now - timedelta(minutes=1)).isoformat(timespec="seconds")
    future = (now + timedelta(minutes=1)).isoformat(timespec="seconds")
    base = registration(_handle())
    assert dataclasses.replace(base, expires_at=past).expired(now) is True
    assert dataclasses.replace(base, expires_at=future).expired(now) is False
    assert dataclasses.replace(base, expires_at=None).expired(now) is False


def test_a_naive_expiry_is_read_as_utc() -> None:
    now = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
    naive = dataclasses.replace(registration(_handle()), expires_at="2026-10-19T11:59:00")
    assert naive.expired(now) is True


def test_a_document_with_unknown_or_missing_fields_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "41.json"
    document = dataclasses.asdict(registration(_handle()))
    with pytest.raises(RegistrationError, match=r"unknown fields \['surprise'\]"):
        decode_registration({**document, "surprise": 1}, path)
    document.pop("machine")
    with pytest.raises(RegistrationError, match=r"misses \['machine'\]"):
        decode_registration(document, path)


# meiva:note
# A handle shaped like the one the bridge mints: 32 random bytes, URL-safe base64. The store keeps
# only its digest, so the value itself is the test's to choose.
def _handle() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
