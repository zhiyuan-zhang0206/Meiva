import os

from base.host.env.agent_env import AGENT_ID_ENV
from base.native_process.root_control import client


def test_the_lifecycle_caller_block_reads_the_agent_id_from_the_launch_key() -> None:
    assert client.caller_identity({AGENT_ID_ENV: "7"}, pid=os.getpid())["agent_id"] == "7"
