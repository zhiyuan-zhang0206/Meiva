import ast
from pathlib import Path
from typing import cast

import pytest
from langchain_core.messages import HumanMessage

from base.paths.tests.homes import point_home
from tests.e2e.fakes.scenarios import shell_effects


async def test_termination_scenario_arms_one_future_at_watcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    point_home(monkeypatch, tmp_path)
    _ = shell_effects.sandbox().mkdir(parents=True, exist_ok=True)
    model = shell_effects.build_watcher_termination("unused", agent_id=17)

    reply = await model.ainvoke([HumanMessage("arm a watcher before termination")])

    assert len(reply.tool_calls) == 1
    code = cast(str, reply.tool_calls[0]["args"]["code"])
    _ = ast.parse(code, filename="<scenario>", mode="exec")
    assert "meiva.watcher.at" in code
    assert "datetime.timedelta(seconds=30)" in code
    assert "TERMINATED-WATCHER-MUST-NOT-FIRE" in code
    assert "termination-watcher" in code
