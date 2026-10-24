# meiva:note
# The Postgres socket directory is one canonical owner-only path per home.
#
# `pg_socket_dir(home)` creates (and keeps) a short `/tmp/meiva-pg-<home-slug>` directory at 0700 —
# a unix socket path is capped near a hundred bytes, so it cannot live under a deep home. The
# admin URL dials only that directory, never a same-port socket elsewhere. The directory is real,
# under a temp socket root of the test's own.

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from base.cluster import home_slug
from base.cluster.dataplane import pg_admin
from cli.commands.data_plane import cluster_instance as instance


def _socket_path(root: Path, home: Path) -> Path:
    return root / f"meiva-pg-{home_slug(home)}"


def test_the_socket_directory_is_keyed_on_the_home_and_owner_only(tmp_path: Path) -> None:
    home = tmp_path / "home"
    socket_dir = instance.pg_socket_dir(home, tmp_path)
    assert socket_dir == _socket_path(tmp_path, home)
    assert socket_dir.stat().st_mode & 0o777 == 0o700


def test_two_homes_never_share_a_socket_directory(tmp_path: Path) -> None:
    first = instance.pg_socket_dir(tmp_path / "one", tmp_path)
    second = instance.pg_socket_dir(tmp_path / "two", tmp_path)
    assert first != second


def test_a_symlinked_socket_directory_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _socket_path(tmp_path, home).symlink_to(tmp_path / "elsewhere", target_is_directory=True)

    with pytest.raises(RuntimeError, match="symlink"):
        instance.pg_socket_dir(home, tmp_path)


def test_a_socket_directory_whose_mode_drifted_is_repaired(tmp_path: Path) -> None:
    home = tmp_path / "home"
    socket_dir = _socket_path(tmp_path, home)
    socket_dir.mkdir()
    socket_dir.chmod(0o755)

    assert instance.pg_socket_dir(home, tmp_path) == socket_dir
    assert socket_dir.stat().st_mode & 0o777 == 0o700


def test_the_admin_url_dials_the_homes_canonical_socket_not_a_same_port_one_elsewhere(
    unit_home: Path, tmp_path: Path
) -> None:
    foreign = tmp_path / "meiva-pg-foreign"
    foreign.mkdir()
    (foreign / ".s.PGSQL.5433").write_text("")

    query = parse_qs(urlsplit(pg_admin.pg_admin_url(5433)).query)

    assert query == {"host": [str(instance.pg_socket_dir(unit_home))], "port": ["5433"]}
