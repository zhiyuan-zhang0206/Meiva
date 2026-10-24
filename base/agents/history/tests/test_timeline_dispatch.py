from typing import NoReturn

import pytest
from langchain_core.messages import HumanMessage

from base.agents.history import timeline
from base.agents.messages.kwargs import MeivaMsgType
from base.agents.messages.timestamp_policy import TimestampPolicy


def test_dispatch_failure_propagates() -> None:
    def fail_dispatch() -> NoReturn:
        raise RuntimeError("simulated message dispatch failure")

    with pytest.raises(RuntimeError, match="simulated message dispatch failure"):
        timeline.build_timeline_items(
            [
                HumanMessage(
                    content="summary",
                    additional_kwargs={"meiva_msg_type": MeivaMsgType.COMPACT_SUMMARY},
                )
            ],
            inputs=TimestampPolicy(clock=fail_dispatch(), timestamps_enabled=True),
        )
