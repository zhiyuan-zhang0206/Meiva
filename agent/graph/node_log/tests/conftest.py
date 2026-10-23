import pytest

from agent.graph import node_log
from agent.graph.node_log.tests.live_events import live_events as live_events
from base.log.tests.log_records import loguru_records as loguru_records


# meiva:note
# The snapshot cursor and the exit buffer are per-process state in production, where one host
# serves many agents. A test session outlives every test, so each test starts from a fresh agent
# process: the first enter takes the full-window path and no earlier test's exits are pending.
@pytest.fixture(autouse=True)
def fresh_process_state() -> None:
    node_log.SNAPSHOT_CURSOR.clear()
    node_log.flush_node_exit_aggregate(1)
