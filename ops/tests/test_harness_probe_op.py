from __future__ import annotations

from base.api_contracts.agent_contract import Harness
from ops.cluster.operations import harness_probe_op


# meiva:note
# One row per harness, always: the spawn form renders a fixed list and the probe's
# absence is reported as `unknown`, never as a missing option.
# meiva:link base/agents/docs/harness.meiva.okf.md
def test_the_op_answers_for_every_known_harness() -> None:
    result = harness_probe_op("box")

    assert result.machine == "box"
    assert [row.harness for row in result.harnesses] == list(Harness)
    assert result.harnesses[0].status == "available"
    assert all(row.reason for row in result.harnesses)
