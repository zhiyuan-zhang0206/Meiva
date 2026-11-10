# meiva:note
# When a question lapses, asserted on values.

from datetime import UTC, datetime, timedelta

from gateway.agents.notice_operations.notice_plan import question_expired

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


def test_question_expiry_is_inclusive_of_the_deadline() -> None:
    assert question_expired(NOW, now=NOW) is True
    assert question_expired(NOW + timedelta(seconds=1), now=NOW) is False


def test_a_question_without_a_deadline_never_lapses() -> None:
    assert question_expired(None, now=NOW) is False
