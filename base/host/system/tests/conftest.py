from pathlib import Path

import pytest

from base.host.system.job_values import JobRoots
from base.paths.tests.homes import default_home as default_home
from base.paths.tests.homes import unit_home as unit_home


@pytest.fixture
def job_roots(tmp_path: Path) -> JobRoots:
    return JobRoots(home=tmp_path / ".meiva", repo=Path("/check/out"), user_home=tmp_path)
