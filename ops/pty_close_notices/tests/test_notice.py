from __future__ import annotations

from dataclasses import replace

from ops.pty_close_notices.notice import (
    CRASH_REASON,
    STOP_REASON,
    ClosureNotice,
    claim_key,
    one_per_key,
)


def _notice(
    *,
    agent_id: int = 7,
    session_id: int = 11,
    birth: str = "starttime:4242",
    survivors: tuple[tuple[int, str], ...] = (),
) -> ClosureNotice:
    return ClosureNotice(
        machine="macmini",
        agent_id=agent_id,
        session_id=session_id,
        name=f"meiva-agent-{agent_id}-shell-{session_id}-report",
        shell_pid=9090,
        shell_birth=birth,
        reason=STOP_REASON,
        closed_at="2026-10-24T00:00:00+00:00",
        survivors=survivors,
    )


# meiva:note
# A notice is about the shell, not about what it ran: the survivors a stop observed and the
# reason it closed do not change the key, so a re-sent notice is one delivery.
def test_the_dedup_key_ignores_survivors_and_reason() -> None:
    first = _notice(survivors=((4242, "sudo"),))
    again = _notice(survivors=((4242, "sudo"), (4343, "python3")))
    crash = replace(first, reason=CRASH_REASON)

    assert first.dedup_key() == again.dedup_key() == crash.dedup_key()
    assert claim_key(first) == f"closure-notice:macmini:{first.dedup_key()}"


# meiva:note
# Every part of the shell's identity is part of the key: a new birth of the same session, another
# session, another agent or another machine is a new notice.
def test_the_dedup_key_changes_with_the_shell_identity() -> None:
    base = _notice()
    other_machine = replace(base, machine="gateway-box")

    keys = {
        base.dedup_key(),
        _notice(birth="starttime:9999").dedup_key(),
        _notice(session_id=12).dedup_key(),
        _notice(agent_id=8).dedup_key(),
        other_machine.dedup_key(),
    }

    assert len(keys) == 5
    assert all(len(key) == 16 for key in keys)


# meiva:note
# The batch spells each key once and the first occurrence wins, in batch order.
def test_one_notice_per_key_first_occurrence_wins() -> None:
    first = _notice(survivors=((1, "first"),))
    again = _notice(survivors=((2, "again"),))
    other = _notice(session_id=12)

    assert one_per_key([first, other, again]) == (first, other)
    assert one_per_key([]) == ()


# meiva:note
# The record names the shell and carries survivors as pid and name objects only when there are
# any; the operator-visible record has no field beyond what the owner is told.
def test_the_record_lists_survivors_only_when_there_are_some() -> None:
    plain = _notice().as_dict()
    with_survivors = _notice(survivors=((4242, "sudo"),)).as_dict()

    assert "survivors" not in plain
    assert with_survivors["survivors"] == [{"pid": 4242, "name": "sudo"}]
    assert plain == {
        "machine": "macmini",
        "agent_id": 7,
        "session_id": 11,
        "name": "meiva-agent-7-shell-11-report",
        "shell_pid": 9090,
        "shell_birth": "starttime:4242",
        "reason": STOP_REASON,
        "closed_at": "2026-10-24T00:00:00+00:00",
    }
