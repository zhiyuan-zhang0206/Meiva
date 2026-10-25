from pathlib import Path

from base.host.env.dotenv_file import process_env_file, read_env_aliases

__all__ = ["unit_env_aliases", "unit_env_path"]


# meiva:note
# The `.env` of the home this test process runs in (the `unit_home` fixture's, when requested).
#
# For a test that drives a writer through the process home and reads back what it wrote.
def unit_env_path() -> Path:
    return process_env_file()


# meiva:note
# The `{ALIAS: value}` of `unit_env_path()` as it is on disk now.
def unit_env_aliases() -> dict[str, str]:
    return read_env_aliases(unit_env_path())
