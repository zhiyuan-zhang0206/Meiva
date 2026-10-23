# meiva:note
# The cluster-timezone context note: the one place the agent is told which timezone its
# timestamps are in. The timestamps themselves carry no `%Z` suffix (see
# `base/agents/messages/tests/test_envelope.py::TestWeekdayFlag::test_no_timezone_suffix_either_way`),
# so the declaration states the zone of the clock the context carries and the offset in
# force at that clock's instant; a cluster on a different timezone is told its own and a
# daylight-saving zone is told the right offset on the day it is read.
from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from langchain_core.messages import BaseMessage

from agent.graph.prompt.context_notes import RANK_CLUSTER_MEMORY, RANK_TIMEZONE, timezone_note
from agent.graph.prompt.note_text import timezone_text
from base.agents.context import MeivaContext
from base.agents.context.identity import AgentIdentity
from base.agents.messages.kwargs import NoteTag
from base.clock import Clock, ClockConfig
from base.clock.tests.manual_timers import ManualTimers

WINTER = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
SUMMER = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def _clock(tz: str, now: datetime = WINTER) -> Clock:
    timers = ManualTimers(wall_start=now)
    return Clock(ClockConfig(tz, tz, False), timers=timers, now=timers.now)


def _context(clock: Clock, agent_id: int | None = 7) -> MeivaContext:
    return MeivaContext(
        identity=AgentIdentity(agent_id=agent_id, owns_loop=True) if agent_id is not None else None,
        clock_factory=lambda: clock,
    )


def _text(note: BaseMessage) -> str:
    content: object = note.content
    assert isinstance(content, str)
    return content


def _content(tz: str, now: datetime = WINTER) -> str:
    note = timezone_note(_context(_clock(tz, now)))
    assert note is not None
    return str(note.content)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]


def test_declares_the_configured_zone_by_iana_name() -> None:
    """The IANA name, not the `%Z` abbreviation — `CST` names both US Central
    and China Standard time, `Asia/Shanghai` names one zone."""
    assert "Asia/Shanghai" in _content("Asia/Shanghai")


def test_offset_is_rendered_from_the_clock() -> None:
    assert "(UTC+08:00)" in _content("Asia/Shanghai")
    assert "(UTC+00:00)" in _content("UTC")


def test_offset_follows_daylight_saving_at_the_clock_instant() -> None:
    assert "America/Los_Angeles (UTC-08:00)" in _content("America/Los_Angeles", WINTER)
    assert "America/Los_Angeles (UTC-07:00)" in _content("America/Los_Angeles", SUMMER)


def test_text_states_name_and_the_offset_of_the_given_instant() -> None:
    now = datetime(2026, 3, 1, 9, 30, tzinfo=ZoneInfo("Asia/Kolkata"))

    text = timezone_text("Asia/Kolkata", now)

    assert text.startswith("Conversation timezone: Asia/Kolkata (UTC+05:30).")


def test_carries_the_timezone_note_tag() -> None:
    """The tag drives the UI chip; an unmapped one renders as a loud alarm
    (`scripts/lint/note_tags.py` enforces the frontend half)."""
    note = timezone_note(_context(_clock("Asia/Shanghai")))
    assert note is not None
    assert note.additional_kwargs["meiva_note_tag"] == NoteTag.TIMEZONE  # pyright: ignore[reportUnknownMemberType]


def test_sits_in_the_stable_cache_band() -> None:
    """Ahead of the shared memory index, and that is a cache decision: the
    index is re-read at every window establishment and changes whenever any
    agent writes memory, so a note behind it re-caches on someone else's write.
    The declaration changes only when `MEIVA_TIMEZONE` does — which already
    forces an agent restart."""
    assert RANK_TIMEZONE < RANK_CLUSTER_MEMORY


def test_opts_out_without_an_agent_identity() -> None:
    """Snapshot renders and the dev REPL have no identity; the note declines
    rather than producing a head fragment out of context."""
    assert timezone_note(_context(_clock("UTC"), None)) is None


def test_renders_under_a_hosted_turn_identity() -> None:
    """The explicit host context works without binding the shared SDK."""
    note = timezone_note(_context(_clock("Asia/Shanghai"), 29))

    assert note is not None
    assert "Asia/Shanghai" in _text(note)


def test_notes_follow_the_clock_of_their_own_context() -> None:
    first = timezone_note(_context(_clock("UTC"), 1))
    second = timezone_note(_context(_clock("Asia/Shanghai"), 2))

    assert first is not None and second is not None
    assert "(UTC+00:00)" in _text(first)
    assert "(UTC+08:00)" in _text(second)


def test_the_clock_is_read_each_time_a_note_is_built() -> None:
    clocks = iter([_clock("UTC"), _clock("America/New_York")])
    context = MeivaContext(
        identity=AgentIdentity(1, True),
        clock_factory=lambda: next(clocks),
    )

    first, second = timezone_note(context), timezone_note(context)

    assert first is not None and second is not None
    assert "UTC (UTC+00:00)" in _text(first)
    assert "America/New_York" in _text(second)
