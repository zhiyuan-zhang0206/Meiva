from __future__ import annotations

import dataclasses
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from meiva.impersonation.harness.attach import attach, descends_from
from meiva.impersonation.harness.registration import (
    FileRegistrationStore,
    HarnessRegistration,
    RegistrationError,
    handle_digest,
    new_handle,
)


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
    record = registration(new_handle())
    store.save(record)
    assert store.load(record.agent_id) == record


def test_store_finds_a_registration_by_its_handle(tmp_path: Path) -> None:
    store = FileRegistrationStore(tmp_path)
    handle = new_handle()
    record = registration(handle)
    store.save(record)
    assert store.find_by_handle(handle) == record
    assert store.find_by_handle(new_handle()) is None


def test_a_registration_records_the_digest_not_the_handle(tmp_path: Path) -> None:
    store = FileRegistrationStore(tmp_path)
    handle = new_handle()
    store.save(registration(handle))
    text = store.path(41).read_text(encoding="utf-8")
    assert handle not in text
    assert handle_digest(handle) in text


def test_expiry_is_read_from_an_iso_timestamp() -> None:
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat(timespec="seconds")
    future = (datetime.now(UTC) + timedelta(minutes=1)).isoformat(timespec="seconds")
    base = registration(new_handle())
    assert dataclasses.replace(base, expires_at=past).expired() is True
    assert dataclasses.replace(base, expires_at=future).expired() is False
    assert dataclasses.replace(base, expires_at=None).expired() is False


def test_a_process_descends_from_itself() -> None:
    assert descends_from(os.getpid()) is True


def test_an_unknown_pid_is_not_an_ancestor() -> None:
    assert descends_from(2**22 + 7) is False
    assert descends_from(0) is False


def test_attach_without_a_handle_says_how_to_get_one() -> None:
    with pytest.raises(RegistrationError) as caught:
        attach("")
    assert "MEIVA_HARNESS_HANDLE" in str(caught.value)


def test_attach_with_an_unknown_handle_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RegistrationError) as caught:
        attach(new_handle(), store=FileRegistrationStore(tmp_path))
    assert "matches this handle" in str(caught.value)
