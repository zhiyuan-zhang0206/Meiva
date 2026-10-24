from zoneinfo import ZoneInfoNotFoundError

import pytest
from langchain_core.messages import HumanMessage

from base.agents.history import timeline
from base.agents.messages.kwargs import MeivaMsgType
from base.agents.messages.timestamp_policy import TimestampPolicy
from base.clock.tests.fakes import clock_with_unknown_timezone


def test_a_render_failure_propagates() -> None:
    with pytest.raises(ZoneInfoNotFoundError):
        timeline.build_timeline_items(
            [
                HumanMessage(
                    content="summary",
                    additional_kwargs={"meiva_msg_type": MeivaMsgType.COMPACT_SUMMARY},
                )
            ],
            inputs=TimestampPolicy.from_clock(
                clock_with_unknown_timezone(), timestamps_enabled=True
            ),
        )
