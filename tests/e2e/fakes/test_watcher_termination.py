import ast
from pathlib import Path
from typing import cast

import pytest
from langchain_core.messages import HumanMessage

from base.paths.tests.homes import point_home
from tests.e2e.fakes.scenarios import shell_effects


async def test_termination_scenario_arms_one_owner_coupled_future_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    point_home(monkeypatch, tmp_path)
    _ = shell_effects.sandbox().mkdir(parents=True, exist_ok=True)
    model = shell_effects.build_watcher_termination("unused", agent_id=17)

    reply = await model.ainvoke([HumanMessage("arm a watcher before termination")])

    assert len(reply.tool_calls) == 1
    code = cast(str, reply.tool_calls[0]["args"]["code"])
    tree = ast.parse(code, filename="<scenario>", mode="exec")
    creates = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "meiva.schedules.at"
    ]
    assert len(creates) == 1
    create = creates[0]
    assert [ast.unparse(arg) for arg in create.args] == ["fire_at"]
    keywords = {keyword.arg: ast.literal_eval(keyword.value) for keyword in create.keywords}
    assert keywords == {
        "message": "TERMINATED-WATCHER-MUST-NOT-FIRE",
        "name": "e2e-terminate",
        "on_owner_terminate": "cancel",
    }
    # meiva:note
    # Schedule ownership defaults to this agent; the persisted handle must be the
    # durable schedule id, which has no shell session or TTL row.
    assert "wid.id" in code
    assert "wid.session_id" not in code
    assert "meiva.watcher" not in code
    assert "datetime.timedelta(seconds=30)" in code
    assert "termination-watcher" in code
