# meiva:note
# `GET /stats` names the home the daemon root handed in and the owner this boot advertises, so a
# maintenance client can tell which cluster's host answered.

import json
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

from base.clock import SYSTEM_TIMERS
from services.agent_runner.agent_host.dispatcher import TurnScheduler
from services.agent_runner.agent_host.health_routes import stats_route
from services.agent_runner.agent_host.host import AgentHost
from services.agent_runner.agent_host.tests.host_policy import (
    configured_machine,
    configured_policy,
    handoff_timestamps,
)


async def test_reports_the_home_pid_and_owner_the_root_handed_in(tmp_path: Path) -> None:
    owner = uuid4()
    host = AgentHost(
        handoff_stamping=handoff_timestamps(),
        home=tmp_path,
        policy=configured_policy(),
        pool=Mock(),
        checkpointer=Mock(),
        graph=Mock(),
        bus=Mock(),
        db=Mock(),
        catalog=Mock(),
        machine=configured_machine(),
        runtime_owner=owner,
    )

    async def idle(_agent_id: int) -> None:
        return None

    scheduler = TurnScheduler(idle, timers=SYSTEM_TIMERS)
    try:
        status, response, _ = await stats_route(host, scheduler, pid=4321, home=tmp_path)(b"")
    finally:
        await scheduler.aclose()

    payload = json.loads(response)
    assert status == 200
    assert payload["home"] == str(tmp_path)
    assert payload["pid"] == 4321
    assert payload["runtime_owner"] == str(owner)
