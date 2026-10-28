# meiva:note
# A foreground server's data directory is compared as one resolved path value.
# meiva:link base/cluster/dataplane/pg_plan.py

from __future__ import annotations

from pathlib import Path

import pytest

from base.cluster.dataplane import pg_plan


def test_the_expected_directory_is_accepted() -> None:
    pg_plan.verify_foreground_data_dir(Path("/cluster/pg/data"), Path("/cluster/pg/data"))


def test_another_directory_is_refused() -> None:
    with pytest.raises(RuntimeError, match="another data directory"):
        pg_plan.verify_foreground_data_dir(Path("/cluster/other/data"), Path("/cluster/pg/data"))


def test_a_server_that_reported_no_directory_is_refused() -> None:
    with pytest.raises(RuntimeError, match="another data directory"):
        pg_plan.verify_foreground_data_dir(None, Path("/cluster/pg/data"))


# meiva:note
# The shell resolves both paths, so the symlink a server was started with proves nothing else.
def test_a_symlinked_spelling_of_the_same_directory_is_accepted(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "data")

    pg_plan.verify_foreground_data_dir((tmp_path / "link").resolve(), (tmp_path / "data").resolve())
