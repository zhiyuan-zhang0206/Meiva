import sys

from langchain_core.messages import HumanMessage

from agent.messages import COMPACT_SUMMARY_HEADER
from base.agents.history.context_breakdown import bucket_messages
from base.agents.history.message_tokens import MessageTokens
from base.lm.system_note import wrap_system_note

if __name__ == "__main__":
    buckets = bucket_messages(
        [HumanMessage(content=wrap_system_note(f"{COMPACT_SUMMARY_HEADER}\n\nbody"))],
        [MessageTokens(7, None, "exact")],
    )
    assert list(buckets) == ["compact_summary"], buckets
    # meiva:note
    # A summary stored before the note carried tags keeps bucketing the same way.
    # meiva:link base/agents/messages/docs/metadata-contracts.meiva.okf.md
    legacy = bucket_messages(
        [HumanMessage(content=f"[system] {COMPACT_SUMMARY_HEADER}\n\nbody")],
        [MessageTokens(7, None, "exact")],
    )
    assert list(legacy) == ["compact_summary"], legacy
    assert "agent.graph" not in sys.modules, "breakdown must not import agent.graph"
