# meiva:note
# The configuration slice of the insights service, as one frozen value.
#
# The process root (`daemon.py`) reads the global configuration once, builds this value and hands it
# to `build_app`; a request handler reads only this value (`request.app.state.insights_config`),
# never a Settings view or the configuration owner.

from __future__ import annotations

from dataclasses import dataclass


# meiva:note
# `run_timeline_message_text_max`: characters of one raw message part served before it is clipped
# (`display.run_timeline_message_text_max`). `default_model`: the model a context breakdown
# resolves when the agent names none (`lm.llm_model`). Timestamp resources are supplied
# separately by the root.
@dataclass(frozen=True)
class InsightsConfig:
    run_timeline_message_text_max: int
    default_model: str
