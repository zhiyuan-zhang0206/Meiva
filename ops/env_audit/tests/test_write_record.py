from __future__ import annotations

from ops.env_audit.redaction import RedactedChange
from ops.env_audit.write_record import WriteFacts, write_event, write_record

_FACTS = WriteFacts(
    ts="2026-10-24T00:00:00+00:00",
    site="ops_config_write",
    pid=4242,
    process="python",
    cmdline="python [arguments redacted]",
    actor="user_session:administrator",
    trace_id="trace-1",
    keys_written=frozenset({"MEIVA_MODEL", "ANTHROPIC_API_KEY"}),
    keys_removed=frozenset({"MEIVA_EXEC_TIMEOUT_SECONDS"}),
    keys_after=("ANTHROPIC_API_KEY", "MEIVA_MODEL"),
    digest_after="d" * 64,
)


# meiva:note
# A write record without a diff keeps the names-only form: no `changed` key at all.
def test_a_record_without_a_diff_has_no_changed_key() -> None:
    record = write_record(_FACTS, None)

    assert "changed" not in record
    assert record == {
        "ts": "2026-10-24T00:00:00+00:00",
        "site": "ops_config_write",
        "pid": 4242,
        "process": "python",
        "cmdline": "python [arguments redacted]",
        "actor": "user_session:administrator",
        "trace_id": "trace-1",
        "keys_written": ["ANTHROPIC_API_KEY", "MEIVA_MODEL"],
        "keys_removed": ["MEIVA_EXEC_TIMEOUT_SECONDS"],
        "keys_after": ["ANTHROPIC_API_KEY", "MEIVA_MODEL"],
        "digest_after": "d" * 64,
    }


# meiva:note
# The diff travels as its redacted entries, in the order the writer produced them.
def test_a_record_carries_the_redacted_diff() -> None:
    changed = (
        RedactedChange("MEIVA_MODEL", "cluster-default", False, "a", "b"),
        RedactedChange("ANTHROPIC_API_KEY", "cluster-default", True, None, None),
    )

    record = write_record(_FACTS, changed)

    assert record["changed"] == [change.as_record() for change in changed]


# meiva:note
# The event stream gets the names and the actor and nothing a value could ride on: no diff, no
# trace id, no key set.
def test_the_event_payload_is_value_free() -> None:
    assert write_event(_FACTS) == {
        "site": "ops_config_write",
        "actor": "user_session:administrator",
        "pid": 4242,
        "process": "python",
        "cmdline": "python [arguments redacted]",
        "keys_written": ["ANTHROPIC_API_KEY", "MEIVA_MODEL"],
        "keys_removed": ["MEIVA_EXEC_TIMEOUT_SECONDS"],
        "digest_after": "d" * 64,
    }
