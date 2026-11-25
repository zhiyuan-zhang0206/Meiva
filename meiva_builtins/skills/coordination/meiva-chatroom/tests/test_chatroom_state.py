from __future__ import annotations

from typing import Any

import pytest

from meiva_builtins.skills.tests.skill_script import load_skill_script

M = load_skill_script(
    "coordination", "meiva-chatroom", "tests", "chatroom_modules.py"
).load_chatroom_modules()
cs = M.state
cr = M.render
ss = M.serial
fence = M.fence
TS = "2026-11-25 13:45"


def _room() -> Any:
    return cs.new_state(
        title="Othello",
        host_agent=6942,
        host_machine="ubuntu",
        host_label="Host",
        members={8901: "Claude", 8902: "GPT", 8903: "Gemini"},
    )


def _post(
    state: Any,
    sender: str,
    *mentions: int,
    all_: bool = False,
    body: str = "hi",
    caller: int | None = None,
) -> Any:
    request = cs.PostRequest(
        sender=sender,
        label=None,
        mentions=tuple(mentions),
        mention_all=all_,
        body=body,
        ts=TS,
        caller=caller,
    )
    return cs.plan_post(state, request)


def test_seq_is_monotonic_and_wakes_only_the_mentioned() -> None:
    state, first, wakes = _post(_room(), "8901", 8903)
    assert (state.seq, first.seq) == (1, 1)
    assert [w.target for w in wakes] == [8903]
    state, second, _ = _post(state, "8903", 8901)
    assert second.seq == 2


def test_sender_is_never_woken_even_when_mentioned_or_mention_all() -> None:
    _, _, wakes = _post(_room(), "8901", 8901, 8902)
    assert [w.target for w in wakes] == [8902]
    _, _, wakes = _post(_room(), "8901", all_=True)
    assert [w.target for w in wakes] == [6942, 8902, 8903]


def test_mentions_are_deduplicated_in_order() -> None:
    _, entry, wakes = _post(_room(), "8901", 8903, 8902, 8903)
    assert entry.mentions == (8903, 8902)
    assert [w.target for w in wakes] == [8903, 8902]


def test_user_posts_wake_members_but_hold_no_cursor() -> None:
    state, entry, wakes = _post(_room(), "user", 8902)
    assert entry.label == "User" and entry.sender == "user"
    assert [w.target for w in wakes] == [8902]
    assert set(state.cursors) == {6942, 8901, 8902, 8903}


def test_caller_is_not_woken_and_keeps_its_cursor() -> None:
    state, _, wakes = _post(_room(), "user", all_=True, caller=6942)
    assert 6942 not in [w.target for w in wakes]
    assert state.cursors[6942] == 0
    assert state.cursors[8902] == 0


def test_delta_covers_everything_since_the_targets_cursor_across_rounds() -> None:
    state, _, _ = _post(_room(), "8901", 8902, body="one")
    state, _, _ = _post(state, "8902", 8901, body="two")
    state, _, wakes = _post(state, "8901", 8903, body="three")
    gemini = wakes[0]
    assert [e.seq for e in gemini.entries] == [1, 2, 3]
    assert state.cursors[8903] == 0
    state = cs.with_acks(state, {8903: 3})
    state, _, wakes = _post(state, "8901", 8903, 8902, body="four")
    by_target = {w.target: [e.seq for e in w.entries] for w in wakes}
    assert by_target == {8903: [4], 8902: [3, 4]}


def test_an_unmentioned_member_keeps_its_backlog_until_woken() -> None:
    state, _, _ = _post(_room(), "8901", 8902)
    state, _, wakes = _post(state, "8902", 8903)
    assert [e.seq for e in wakes[0].entries] == [1, 2]
    assert state.cursors[8901] == 1 and state.cursors[8902] == 2 and state.cursors[8903] == 0


@pytest.mark.parametrize("from_start,cursor", [(False, 2), (True, 0)])
def test_new_member_cursor_starts_at_the_present_or_the_beginning(
    from_start: bool, cursor: int
) -> None:
    state, _, _ = _post(_room(), "8901", 8902)
    state, _, _ = _post(state, "8902", 8901)
    state = cs.with_member(state, 9000, "Late", from_start=from_start)
    assert state.cursors[9000] == cursor
    _, _, wakes = _post(state, "8901", 9000)
    assert [e.seq for e in wakes[0].entries] == list(range(cursor + 1, 4))


def test_invalid_posts_fail_fast() -> None:
    state = _room()
    with pytest.raises(ValueError, match="not a member"):
        _post(state, "7777", 8901)
    with pytest.raises(ValueError, match="not members"):
        _post(state, "8901", 7777)
    with pytest.raises(ValueError, match="non-empty"):
        _post(state, "8901", 8902, body="  \n")
    with pytest.raises(ValueError, match="agent id"):
        _post(state, "someone", 8902)
    with pytest.raises(ValueError, match="already a member"):
        cs.with_member(state, 8901, "again", from_start=False)


def test_state_round_trips_through_json() -> None:
    state, _, _ = _post(_room(), "8901", 8902, body="line one\n\nline *two*")
    assert ss.load_state(ss.dump_state(state)) == state


def test_entry_rendering_names_sender_and_targets() -> None:
    state, entry, _ = _post(_room(), "8903", 8901, 8902, body="claim")
    assert (
        cr.render_entry(entry)
        == "\n---\n### #001 · 2026-11-25 13:45 · Gemini (#8903) -> @8901, @8902\n\nclaim\n"
    )
    _, everyone, _ = _post(state, "user", all_=True)
    assert "· User -> @all" in cr.render_entry(everyone)
    _, quiet, _ = _post(state, "8901")
    assert cr.render_entry(quiet).splitlines()[2].endswith("Claude (#8901)")


def test_select_entries_by_since_and_tail() -> None:
    state = _room()
    for n in range(5):
        state, _, _ = _post(state, "8901", 8902, body=str(n))
    assert [e.seq for e in cs.select_entries(state.entries, since=3, tail=None)] == [4, 5]
    assert [e.seq for e in cs.select_entries(state.entries, since=0, tail=2)] == [4, 5]
    assert [e.seq for e in cs.select_entries(state.entries, since=0, tail=0)] == []


def test_wake_targets_keep_their_cursor_until_acknowledged() -> None:
    state, _, wakes = _post(_room(), "8901", 8902, 8903)
    assert state.cursors == {6942: 0, 8901: 1, 8902: 0, 8903: 0}
    state = cs.with_acks(state, {8902: 1})
    assert state.cursors[8902] == 1 and state.cursors[8903] == 0
    assert cs.with_acks(state, {8902: 0}).cursors[8902] == 1
    _, _, again = _post(state, "8901", 8902, 8903, body="two")
    assert {w.target: [e.seq for e in w.entries] for w in again} == {8902: [2], 8903: [1, 2]}
    assert len(wakes) == 2


def test_acks_reject_strangers_and_the_future() -> None:
    state, _, _ = _post(_room(), "8901", 8902)
    with pytest.raises(ValueError, match="not members"):
        cs.with_acks(state, {7777: 1})
    with pytest.raises(ValueError, match="beyond"):
        cs.with_acks(state, {8902: 2})


def test_an_entry_never_turns_the_previous_paragraph_into_a_setext_heading() -> None:
    state, first, _ = _post(_room(), "8901", 8902, body="first para")
    _, second, _ = _post(state, "8902", 8901, body="second para")
    text = cr.render_entry(first) + cr.render_entry(second)
    lines = text.splitlines()
    rules = [i for i, line in enumerate(lines) if line == "---"]
    assert len(rules) == 2
    assert all(lines[i - 1] == "" for i in rules)
    assert "first para\n\n---\n### #002" in text


@pytest.mark.parametrize(
    "body,closed",
    [
        ("plain", "plain"),
        ("```py\nx = 1\n```", "```py\nx = 1\n```"),
        ("```py\nx = 1", "```py\nx = 1\n```"),
        ("~~~\nx", "~~~\nx\n~~~"),
        ("````\n```\nx", "````\n```\nx\n````"),
        ("~~~\n```\nx\n```", "~~~\n```\nx\n```\n~~~"),
        ("```\na\n```\n```\nb", "```\na\n```\n```\nb\n```"),
        ("   ```\nx", "   ```\nx\n```"),
        ("inline ``` not a fence", "inline ``` not a fence"),
        ("``` has `tick`\nx", "``` has `tick`\nx"),
    ],
)
def test_close_open_fence_follows_commonmark(body: str, closed: str) -> None:
    assert fence.close_open_fence(body) == closed


def test_a_post_with_an_unclosed_fence_cannot_swallow_the_next_entry() -> None:
    state, first, _ = _post(_room(), "8901", 8902, body="look:\n```python\nx = 1")
    _, second, _ = _post(state, "8902", 8901, body="reply")
    assert first.body == "look:\n```python\nx = 1\n```"
    assert fence.close_open_fence(first.body) == first.body
    text = cr.render_entry(first) + cr.render_entry(second)
    assert text.index("\n```\n") < text.index("### #002")
