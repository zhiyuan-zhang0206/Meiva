# meiva:note
# The `.env` audit shell against real files: every test hands the audited `.env` path to the
# code as a value, so no test switches `MEIVA_HOME`, and the writers of `base` run unpatched.
# The registry that decides which aliases are sensitive is the real one.

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal, assert_never, cast

import psutil
import pytest

from base.config import runtime_config
from base.host.env import dotenv_file
from base.host.env.dotenv_file import EnvAudit
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from ops import env_audit as audit
from ops.env_audit import (
    EnvWriteAudit,
    check_env_integrity,
    read_env_write_records,
    record_env_write,
)

_MACHINE = "test-machine"


@pytest.fixture
def env_path(tmp_path: Path) -> Path:
    return tmp_path / ".env"


def _audit_path(env_path: Path) -> Path:
    return env_path.with_name(".env.audit.jsonl")


def _armed_path(env_path: Path) -> Path:
    return env_path.with_name(".env.audit.armed")


def _audited_write(env_path: Path, **updates: str) -> None:
    runtime_config.write_fields(
        updates or {"llm_model": "test-model"}, set(), path=env_path, audit=EnvWriteAudit("test")
    )


def _records(env_path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in _audit_path(env_path).read_text().splitlines()]


# meiva:note
# The newest record of the history beside `env_path`, or None while it has none.
def _last_record(env_path: Path) -> dict[str, object] | None:
    records = read_env_write_records(1, env_path)
    return records[0] if records else None


def _check(env_path: Path) -> dict[str, object] | None:
    return check_env_integrity(env_path=env_path, machine_name=_MACHINE)


# meiva:note
# Removing the JSONL append or leaking a value makes this regression fail.
def test_record_env_write_records_metadata_without_config_values(env_path: Path) -> None:
    written_value = "audit-test-value"
    env_path.write_text(f"MEIVA_MODEL={written_value}\n")

    record_env_write(env_path, {"MEIVA_MODEL"}, set(), site="test")

    audit_path = _audit_path(env_path)
    line = audit_path.read_text()
    record = json.loads(line)
    assert audit_path.stat().st_mode & 0o777 == 0o600
    assert _armed_path(env_path).stat().st_mode & 0o777 == 0o600
    assert record["site"] == "test"
    assert record["keys_written"] == ["MEIVA_MODEL"]
    assert record["keys_removed"] == []
    assert record["digest_after"] == hashlib.sha256(env_path.read_bytes()).hexdigest()
    assert {"ts", "pid", "process", "cmdline"} <= set(record)
    assert written_value not in line
    assert _last_record(env_path) == record


# meiva:note
# The record names the real process by its executable alone: no argument of the test runner's
# command line reaches the audit, because an argument can carry a configuration value.
def test_the_recorded_command_line_is_the_executable_alone(env_path: Path) -> None:
    env_path.write_text("MEIVA_MODEL=test-model\n")
    own = psutil.Process()

    record_env_write(env_path, {"MEIVA_MODEL"}, set(), site="test")

    record = _last_record(env_path)
    assert record is not None
    assert record["process"] == own.name()
    assert record["cmdline"] == f"{Path(own.cmdline()[0]).name} [arguments redacted]"


# meiva:note
# Removing the post-write record must make the guard report a mismatch.
def test_audited_runtime_write_leaves_integrity_healthy(env_path: Path) -> None:
    _audited_write(env_path)

    assert _check(env_path) is None


# meiva:note
# Recording typed field names instead of shared `.env` keys must fail.
def test_audited_runtime_write_records_env_aliases(env_path: Path) -> None:
    _audited_write(env_path)

    record = _last_record(env_path)

    assert record is not None
    assert record["keys_written"] == ["MEIVA_MODEL"]


@pytest.mark.parametrize(
    ("damage", "reason"),
    [
        ("deleted", "audit_history_missing"),
        ("emptied", "audit_history_empty"),
        ("corrupted", "audit_history_corrupt"),
        ("digestless", "audit_history_missing_digest"),
    ],
)
# meiva:note
# Damaging the history of an armed home must be reported and repaired, and never return the home
# to the fresh-home unarmed state: the next check is clean because the baseline was rebuilt.
def test_an_armed_guard_rebuilds_a_damaged_history(
    env_path: Path, damage: str, reason: str
) -> None:
    _audited_write(env_path)
    history = _audit_path(env_path)
    match damage:
        case "deleted":
            history.unlink()
        case "emptied":
            history.write_text("")
        case "corrupted":
            history.write_text("not-json\n")
        case "digestless":
            history.write_text('{"site":"test"}\n')
        case _:
            raise AssertionError(damage)

    detection = _check(env_path)

    assert detection is not None
    assert detection["kind"] == "unauthorized"
    assert detection["reason"] == reason
    assert _last_record(env_path) == detection
    assert _check(env_path) is None


# meiva:note
# Removing anomaly recording or its digest self-rate-limit breaks this test.
def test_the_integrity_guard_records_one_out_of_band_write(env_path: Path) -> None:
    _audited_write(env_path)
    env_path.write_text(env_path.read_text() + "MEIVA_EXEC_TIMEOUT_SECONDS=12\n")

    detection = _check(env_path)

    assert detection is not None
    assert detection["kind"] == "unauthorized"
    assert detection["last_official_site"] == "test"
    assert detection["keys"] == ["MEIVA_EXEC_TIMEOUT_SECONDS"]
    assert _check(env_path) is None
    assert _records(env_path)[-1]["kind"] == "unauthorized"


# meiva:note
# A history that predates the armed marker is upgraded by writing the marker, not reported.
def test_a_history_without_a_marker_is_upgraded_without_an_anomaly(env_path: Path) -> None:
    _audited_write(env_path)
    _armed_path(env_path).unlink()

    assert _check(env_path) is None
    assert _armed_path(env_path).exists()


# meiva:note
# The anomaly reaches the event stream and the log with the machine label the caller named,
# after the baseline landed.
def test_an_anomaly_is_emitted_with_the_machine_label(
    env_path: Path, emitted_events: EmittedEvents, loguru_records: LogRecords
) -> None:
    _audited_write(env_path)
    env_path.write_text(env_path.read_text() + "MEIVA_EXEC_TIMEOUT_SECONDS=12\n")

    _check(env_path)

    (attributes,) = emitted_events.attributes("env_unauthorized_write")
    assert attributes["machine"] == _MACHINE
    assert attributes["last_official_site"] == "test"
    assert attributes["keys"] == ["MEIVA_EXEC_TIMEOUT_SECONDS"]
    assert any("out-of-band .env modification" in m for m in loguru_records.messages("ERROR"))


# meiva:note
# Creating an audit record for a fresh home must make this test fail.
def test_the_integrity_guard_is_unarmed_without_an_audit_file(env_path: Path) -> None:
    env_path.write_text("MEIVA_MODEL=test-model\n")

    assert _check(env_path) is None
    assert not _audit_path(env_path).exists()
    assert not _armed_path(env_path).exists()


# meiva:note
# An error while inspecting the files never fails a config read: it is logged and the guard
# answers "no anomaly".
def test_an_unreadable_history_location_does_not_fail_the_check(
    env_path: Path, loguru_records: LogRecords
) -> None:
    _audited_write(env_path)
    history = _audit_path(env_path)
    history.unlink()
    history.mkdir()

    assert _check(env_path) is None
    assert any("could not inspect .env audit integrity" in m for m in loguru_records.messages())


# meiva:note
# Ignoring the opt-in audit site must make this compatibility test fail.
def test_runtime_write_without_audit_keeps_audit_unarmed(env_path: Path) -> None:
    runtime_config.write_fields({"llm_model": "test-model"}, set(), path=env_path)

    assert not _audit_path(env_path).exists()


# meiva:note
# An empty patch must not create an actor record or armed marker.
def test_empty_audited_runtime_write_keeps_audit_unarmed(env_path: Path) -> None:
    runtime_config.write_fields({}, set(), path=env_path, audit=EnvWriteAudit("test"))

    assert not _audit_path(env_path).exists()
    assert not _armed_path(env_path).exists()


# meiva:note
# A malformed non-value byte must not prevent the record from landing.
def test_record_env_write_tolerates_non_utf8_env_bytes(env_path: Path) -> None:
    env_path.write_bytes(b"MEIVA_MODEL=test-model\n\xff\n")

    record_env_write(env_path, {"MEIVA_MODEL"}, set(), site="test")

    record = _last_record(env_path)
    assert record is not None
    assert record["keys_after"] == ["MEIVA_MODEL"]


# meiva:note
# The audited key-name surface matches the settings parser: `export KEY=v` records KEY (never
# `export KEY`), and comments and bare keys stay out (#2981).
def test_env_key_names_read_export_prefixed_assignments(env_path: Path) -> None:
    env_path.write_text("export MEIVA_DB_URL=postgresql://x\n# export MEIVA_SKIP_ME=1\nBARE_KEY\n")

    assert audit.env_key_names(env_path) == ["MEIVA_DB_URL"]


# meiva:note
# The v2 `changed` diff keeps values only for `sensitive: false` registered fields, read from
# the real registry.
def test_record_env_write_redacts_sensitive_and_unregistered_changes(env_path: Path) -> None:
    env_path.write_text("MEIVA_MODEL=test-model\nANTHROPIC_API_KEY=sk-old\nEXTRA_UNKNOWN_KEY=x\n")
    record_env_write(
        env_path,
        {"MEIVA_MODEL", "ANTHROPIC_API_KEY", "EXTRA_UNKNOWN_KEY"},
        set(),
        site="test",
        actor="user_session:administrator",
        trace_id="trace-1",
        changes=[
            {"alias": "MEIVA_MODEL", "old": "old-model", "new": "test-model"},
            {"alias": "ANTHROPIC_API_KEY", "old": "sk-old", "new": "sk-SECRET-NEW"},
            {"alias": "EXTRA_UNKNOWN_KEY", "old": "x", "new": "y"},
        ],
    )

    line = _audit_path(env_path).read_text()
    record = _last_record(env_path)
    assert record is not None
    assert record["actor"] == "user_session:administrator"
    assert record["trace_id"] == "trace-1"
    entries = cast("list[dict[str, object]]", record["changed"])
    changed = {str(entry["alias"]): entry for entry in entries}
    assert changed["MEIVA_MODEL"] == {
        "alias": "MEIVA_MODEL",
        "scope": "cluster-default",
        "sensitive": False,
        "old": "old-model",
        "new": "test-model",
    }
    assert changed["ANTHROPIC_API_KEY"]["sensitive"] is True
    assert changed["ANTHROPIC_API_KEY"]["old"] is None
    assert changed["ANTHROPIC_API_KEY"]["new"] is None
    assert changed["EXTRA_UNKNOWN_KEY"]["scope"] is None
    assert changed["EXTRA_UNKNOWN_KEY"]["sensitive"] is None
    assert "sk-SECRET-NEW" not in line and "sk-old" not in line


# meiva:note
# A second write records the actual old to new transition and the actor.
def test_write_fields_records_old_to_new_values(env_path: Path) -> None:
    runtime_config.write_fields(
        {"llm_model": "m1"}, set(), path=env_path, audit=EnvWriteAudit("test")
    )
    runtime_config.write_fields(
        {"llm_model": "m2"},
        set(),
        path=env_path,
        audit=EnvWriteAudit(site="test", actor="cluster_bearer:administrator"),
    )

    records = _records(env_path)
    assert records[-1]["actor"] == "cluster_bearer:administrator"
    assert records[-1]["changed"] == [
        {
            "alias": "MEIVA_MODEL",
            "scope": "cluster-default",
            "sensitive": False,
            "old": "m1",
            "new": "m2",
        }
    ]


# meiva:note
# A repeated byte-identical upsert must not manufacture an audit record (the converge noise of
# task #3637: every `meiva start` presents the same app port, and a boot-retry storm re-runs it).
# The chain stays whole: a real change records its old to new diff and the guard sees a
# consistent file after both paths.
def test_a_noop_upsert_leaves_no_record_and_a_real_change_still_records(env_path: Path) -> None:
    env_path.write_text("MEIVA_MODEL=first-model\n")

    dotenv_file.upsert_env(env_path, {"MEIVA_MODEL": "second-model"}, audit=EnvWriteAudit("test"))
    after_change = _audit_path(env_path).read_text().splitlines()
    assert len(after_change) == 1

    dotenv_file.upsert_env(
        env_path, {"MEIVA_MODEL": "second-model"}, audit=EnvWriteAudit("converge_gateway_port")
    )

    assert _audit_path(env_path).read_text().splitlines() == after_change
    assert env_path.read_text() == "MEIVA_MODEL=second-model\n"
    assert _check(env_path) is None

    dotenv_file.upsert_env(env_path, {"MEIVA_MODEL": "first-model"}, audit=EnvWriteAudit("test"))

    record = _last_record(env_path)
    assert record is not None
    assert record["keys_written"] == ["MEIVA_MODEL"]
    assert record["changed"] == [
        {
            "alias": "MEIVA_MODEL",
            "scope": "cluster-default",
            "sensitive": False,
            "old": "second-model",
            "new": "first-model",
        }
    ]
    assert _check(env_path) is None


# meiva:note
# A no-op is not a write: it neither records nor arms. A fresh home arms at its first CHANGING
# write, because the no-op cannot vouch for a file it did not write.
def test_a_noop_upsert_does_not_arm_a_fresh_home(env_path: Path) -> None:
    env_path.write_text("MEIVA_MODEL=test-model\n")
    audit_site = EnvWriteAudit("converge_gateway_port")

    dotenv_file.upsert_env(env_path, {"MEIVA_MODEL": "test-model"}, audit=audit_site)

    assert not _audit_path(env_path).exists()
    assert not _armed_path(env_path).exists()

    dotenv_file.upsert_env(env_path, {"MEIVA_MODEL": "other-model"}, audit=audit_site)

    assert _armed_path(env_path).exists()


# meiva:note
# A secret write lands by name only: its value never reaches the JSONL.
def test_write_fields_withholds_sensitive_values(env_path: Path) -> None:
    runtime_config.write_fields(
        {"deepseek_api_key": "sk-SECRET-VALUE"}, set(), path=env_path, audit=EnvWriteAudit("test")
    )

    assert "sk-SECRET-VALUE" not in _audit_path(env_path).read_text()
    record = _last_record(env_path)
    assert record is not None
    entry = cast("list[dict[str, object]]", record["changed"])[0]
    assert entry["sensitive"] is True
    assert entry["old"] is None and entry["new"] is None


# meiva:note
# The value-free event stream also gains the initiating actor.
def test_env_write_event_carries_actor_without_values(
    env_path: Path, emitted_events: EmittedEvents
) -> None:
    runtime_config.write_fields(
        {"llm_model": "m3"},
        set(),
        path=env_path,
        audit=EnvWriteAudit(site="test", actor="user_session:administrator"),
    )

    captured = emitted_events.named("env_write")
    assert captured
    payload = captured[0].attributes
    assert payload["actor"] == "user_session:administrator"
    assert "m3" not in json.dumps(payload)


# meiva:note
# Redaction runs before any audit artifact exists: a diff entry the redactor cannot read fails
# the record, and neither the history nor the marker appears (direct recording cannot undo the
# caller's already-landed bytes).
def test_a_diff_that_cannot_be_redacted_leaves_no_audit_artifacts(env_path: Path) -> None:
    env_path.write_text("MEIVA_MODEL=new-value\n")

    with pytest.raises(KeyError, match="alias"):
        record_env_write(
            env_path,
            {"MEIVA_MODEL"},
            set(),
            site="test",
            changes=[{"old": "old-value", "new": "new-value"}],
        )

    assert env_path.read_text() == "MEIVA_MODEL=new-value\n"
    assert not _audit_path(env_path).exists()
    assert not _armed_path(env_path).exists()


# meiva:note
# The declaration projection reads the real registry: the scope and sensitivity each alias
# declares.
def test_alias_metadata_projects_the_registry_declarations() -> None:
    projected = audit.load_alias_metadata()

    assert projected["MEIVA_MODEL"] == ("cluster-default", False)
    assert projected["MEIVA_DB_URL"].sensitive is True


# meiva:note
# The projection works in a process with no runtime settings image and without importing the
# full config models or the panel metadata, so a repair command can audit before settings load.
# The child's argv is fixed: this interpreter runs the script beside this test.
def test_declaration_projection_works_without_a_runtime_settings_image(tmp_path: Path) -> None:
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("_projection_child.py")),
        ],
        cwd=tmp_path,
        env={
            **{key: value for key, value in os.environ.items() if key != "MEIVA_CONFIG_BOOT"},
            "MEIVA_HOME": str(tmp_path / "absent-home"),
            "MEIVA_CONFIG_FETCH": "skip",
            "MEIVA_PROCESS_PROFILE": "runner",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


_Mode = Literal["failing", "forbidden", "recording"]


# meiva:note
# An `EnvAudit` for the writers of `base` that stands where a writer calls `prepare`: in
# `recording` mode it keeps the bytes the `.env` held at that moment and the diff it was handed;
# `failing` raises from `prepare`; `forbidden` fails the test if `prepare` is called at all. The
# writers own the order (prepare while the old bytes are present, before any change, never for
# a write that changes nothing); these tests pin that order for the audit protocol.
class _WriterAudit:
    def __init__(self, env_path: Path, *, mode: _Mode) -> None:
        self._env_path = env_path
        self._mode: _Mode = mode
        self.prepared_with: list[str] = []

    def prepare(self, changes: Sequence[Mapping[str, object]]) -> object:
        match self._mode:
            case "forbidden":
                pytest.fail("the writer prepared audit metadata when it must not have")
            case "failing":
                raise RuntimeError("registry unavailable")
            case "recording":
                self.prepared_with.append(self._env_path.read_text())
                return changes
            case _:
                assert_never(self._mode)

    def record(self, path: Path, written: set[str], removed: set[str], prepared: object) -> None:
        if self._mode != "recording":
            pytest.fail("a writer recorded a write whose audit was failing or forbidden")


def _write_with(writer: str, env_path: Path, audit_for_write: EnvAudit) -> None:
    match writer:
        case "fields-update":
            runtime_config.write_fields(
                {"llm_model": "after"}, set(), path=env_path, audit=audit_for_write
            )
        case "fields-remove":
            runtime_config.write_fields({}, {"llm_model"}, path=env_path, audit=audit_for_write)
        case "upsert":
            dotenv_file.upsert_env(env_path, {"MEIVA_MODEL": "after"}, audit=audit_for_write)
        case "remove":
            dotenv_file.remove_env(env_path, {"MEIVA_MODEL"}, audit=audit_for_write)
        case _:
            raise AssertionError(writer)


@pytest.mark.parametrize("writer", ["fields-update", "fields-remove", "upsert", "remove"])
def test_a_failed_prepare_prevents_every_audited_file_write(env_path: Path, writer: str) -> None:
    # meiva:note
    # A prepare that fails prevents every audited file write: the `.env`, its backups and the
    # audit files are all untouched.
    original = b"MEIVA_MODEL=before\nUNREGISTERED_SECRET=secret-before\n"
    env_path.write_bytes(original)

    with pytest.raises(RuntimeError, match="registry unavailable"):
        _write_with(writer, env_path, _WriterAudit(env_path, mode="failing"))

    assert env_path.read_bytes() == original
    assert not (env_path.parent / "backups").exists()
    assert not _audit_path(env_path).exists()
    assert not _armed_path(env_path).exists()


@pytest.mark.parametrize("writer", ["fields", "upsert", "remove"])
def test_a_noop_writer_does_not_prepare_audit_metadata(env_path: Path, writer: str) -> None:
    # meiva:note
    # A write that changes nothing never prepares audit metadata.
    original = b"MEIVA_MODEL=before\n"
    env_path.write_bytes(original)
    forbidden = _WriterAudit(env_path, mode="forbidden")

    match writer:
        case "fields":
            runtime_config.write_fields({}, set(), path=env_path, audit=forbidden)
        case "upsert":
            dotenv_file.upsert_env(env_path, {"MEIVA_MODEL": "before"}, audit=forbidden)
        case "remove":
            dotenv_file.remove_env(env_path, {"ABSENT"}, audit=forbidden)
        case _:
            raise AssertionError(writer)

    assert env_path.read_bytes() == original
    assert not _audit_path(env_path).exists()
    assert not (env_path.parent / "backups").exists()


# meiva:note
# A stale expected digest fails before the audit metadata is prepared.
def test_a_stale_digest_fails_before_metadata_preparation(env_path: Path) -> None:
    original = b"MEIVA_MODEL=before\n"
    env_path.write_bytes(original)
    forbidden = _WriterAudit(env_path, mode="forbidden")

    with pytest.raises(RuntimeError, match="changed before owned"):
        runtime_config.write_fields(
            {"llm_model": "after"}, set(), path=env_path, audit=forbidden, expected_digest="stale"
        )

    assert env_path.read_bytes() == original
    assert not _audit_path(env_path).exists()
    assert not (env_path.parent / "backups").exists()


@pytest.mark.parametrize("writer", ["fields-update", "upsert", "remove"])
def test_redaction_is_prepared_once_while_the_old_env_is_still_present(
    env_path: Path, writer: str
) -> None:
    # meiva:note
    # The writer prepares the diff exactly once, while the old `.env` bytes are still present.
    original = "MEIVA_MODEL=before\n"
    env_path.write_text(original)
    recording = _WriterAudit(env_path, mode="recording")

    _write_with(writer, env_path, recording)

    assert recording.prepared_with == [original]


@pytest.mark.parametrize("writer", ["fields-update", "upsert", "remove"])
def test_every_writer_records_the_old_to_new_transition(env_path: Path, writer: str) -> None:
    # meiva:note
    # Every official writer leaves the old to new transition of the registered alias.
    env_path.write_text("MEIVA_MODEL=before\n")

    _write_with(writer, env_path, EnvWriteAudit("test"))

    record = _last_record(env_path)
    assert record is not None
    assert record["changed"] == [
        {
            "alias": "MEIVA_MODEL",
            "scope": "cluster-default",
            "sensitive": False,
            "old": "before",
            "new": None if writer == "remove" else "after",
        }
    ]


# meiva:note
# The newest records come first and `limit` bounds the listing.
def test_read_env_write_records_returns_newest_first_with_limit(env_path: Path) -> None:
    env_path.write_text("MEIVA_MODEL=m\n")
    for i in range(3):
        record_env_write(env_path, {"MEIVA_MODEL"}, set(), site=f"test-{i}")

    records = audit.read_env_write_records(2, env_path)

    assert [record["site"] for record in records] == ["test-2", "test-1"]


# meiva:note
# Corrupt lines are skipped and a missing history lists nothing.
def test_read_env_write_records_skips_corrupt_lines_and_missing_history(env_path: Path) -> None:
    env_path.write_text("MEIVA_MODEL=m\n")
    record_env_write(env_path, {"MEIVA_MODEL"}, set(), site="good")
    with _audit_path(env_path).open("a") as fh:
        fh.write("{not json\n")
        fh.write("[1, 2]\n")

    records = audit.read_env_write_records(10, env_path)
    assert [record["site"] for record in records] == ["good"]

    elsewhere = env_path.parent / "elsewhere" / ".env"
    elsewhere.parent.mkdir()
    assert audit.read_env_write_records(5, elsewhere) == []


# meiva:note
# A history larger than the tail window still serves its newest records.
def test_read_env_write_records_reads_beyond_a_full_tail_window(env_path: Path) -> None:
    with _audit_path(env_path).open("w") as fh:
        for i in range(400):
            fh.write(json.dumps({"site": f"s{i}", "pad": "x" * 200}) + "\n")

    records = audit.read_env_write_records(3, env_path)

    assert [record["site"] for record in records] == ["s399", "s398", "s397"]


# meiva:note
# A listing of zero or fewer records is a caller bug.
def test_read_env_write_records_rejects_nonpositive_limit(env_path: Path) -> None:
    with pytest.raises(ValueError, match="limit"):
        audit.read_env_write_records(0, env_path)


# meiva:note
# An unreadable record in the tail window is reported, not silently dropped.
def test_a_corrupt_history_line_is_reported_not_silently_dropped(
    env_path: Path, loguru_records: LogRecords
) -> None:
    env_path.write_text("A=1\n", encoding="utf-8")
    _audit_path(env_path).write_text(
        '{"actor": "t", "keys": ["A"]}\n{"actor": "t", "keys": [\n', encoding="utf-8"
    )

    records = audit.read_env_write_records(5, env_path)

    assert len(records) == 1
    [event] = loguru_records.events("env_audit_history")
    assert event["extra"]["n"] == 1
    assert ".env.audit.jsonl" in event["message"]
