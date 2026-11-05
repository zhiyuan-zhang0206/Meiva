import os
from pathlib import Path

from base.host.env.agent_env import AGENT_ID_ENV, TEMP_DIR_KEYS, temp_dir_of
from base.native_process.root_control import client


def test_the_lifecycle_caller_block_reads_the_agent_id_from_the_launch_key() -> None:
    assert client.caller_identity({AGENT_ID_ENV: "7"}, pid=os.getpid())["agent_id"] == "7"


# meiva:note
# The POSIX fallback `temp_dir_of` returns when the dict names no temp key, written once so the cases
# below read as cases rather than as three copies of the same path.
FALLBACK = Path("/tmp")  # noqa: S108


def test_the_temp_root_is_the_first_temp_key_the_dict_names() -> None:
    assert TEMP_DIR_KEYS[0] == "TMPDIR"
    assert temp_dir_of({"TMPDIR": "/var/folders/x", "TEMP": "/elsewhere"}) == Path("/var/folders/x")
    assert temp_dir_of({"TEMP": "/elsewhere"}) == Path("/elsewhere")
    assert temp_dir_of({}) == FALLBACK


def test_a_temp_key_holding_nothing_names_no_directory() -> None:
    assert temp_dir_of({"TMPDIR": ""}) == FALLBACK
