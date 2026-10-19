from pathlib import Path

from cli.commands.data_plane import cluster_instance as instance


def test_pg_bin_uses_the_shared_resolver() -> None:
    assert instance.pg_bin("initdb", tool=lambda name: Path("/vendored/bin") / name) == (
        "/vendored/bin/initdb"
    )
