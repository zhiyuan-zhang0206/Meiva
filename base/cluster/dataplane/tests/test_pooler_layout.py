from __future__ import annotations

from pathlib import Path

from base.cluster.dataplane import pooler


def test_pooler_files_live_in_the_pgbouncer_directory_under_the_home(
    tmp_path: Path,
) -> None:
    assert not (tmp_path / "pgbouncer").exists()
    assert pooler.ini_path(tmp_path) == tmp_path / "pgbouncer" / "pgbouncer.ini"
    assert pooler.pidfile_path(tmp_path) == tmp_path / "pgbouncer" / "pgbouncer.pid"
    assert (tmp_path / "pgbouncer").is_dir()
