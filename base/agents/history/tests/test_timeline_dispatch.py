from typing import NoReturn

import pytest
from langchain_core.messages import HumanMessage

from base.agents.history import timeline
from base.agents.history.timeline_inputs import TimelineReadInputs
from base.agents.messages.kwargs import MeivaMsgType


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
            inputs=TimelineReadInputs(fail_dispatch, lambda: True),
        )
