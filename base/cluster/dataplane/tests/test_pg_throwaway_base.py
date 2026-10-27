"""Base selection for throwaway Postgres clusters (`base/cluster/dataplane/pg_throwaway_base.py`).

The platform default is `/dev/shm` on Linux — RAM-sized — while a full-restore
drill needs disk-sized room: on 2026-09-14 the throwaway postmaster died
mid-restore when the cluster outgrew a 7.8 GiB /dev/shm (dmesg signal 6; the
drill surfaced only `PQputCopyData: server closed the connection`), and on
2026-10-05 a 15.4 GiB estimate cleared /dev/shm's 16 GiB while the actual restore
did not. A caller that declares its footprint therefore takes the durable disk
fallback outright; these tests exercise selection through its seams — the
explicit override value, the per-test-pinned `default_base`, and a
scripted `free_bytes` — so they never depend on a real tmpfs's size or a host's
real configured base.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from base.cluster.dataplane import pg_plan, pg_tools
from base.cluster.dataplane import pg_throwaway_base as base
from base.cluster.dataplane.pg_throwaway_base import ThrowawayConfig
from base.paths import meiva_home


@pytest.fixture
def scratch_bases(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    """A default base and a disk fallback pinned at scratch dirs, with the operator
    override supplied by each test — a host's real config must not leak in."""
    default = tmp_path / "shm"
    fallback = tmp_path / "var-tmp"
    default.mkdir()
    fallback.mkdir()
    yield default, fallback


def test_no_requirement_keeps_the_platform_default_without_measuring(
    scratch_bases: tuple[Path, Path],
) -> None:
    """The historical behavior, byte for byte: callers that do not declare a
    footprint (every test fixture, the migration smoke) never touch disk_usage."""
    default, fallback = scratch_bases
    chosen, warning = pg_plan.choose_throwaway_base(
        None,
        override=None,
        default=default,
        fallback=fallback,
        free_space={},
    )
    assert chosen == default
    assert warning is None
    assert base.select_throwaway_base(override=None, default_root=default) == default
    assert base.select_throwaway_base(None, override=None, default_root=default) == default


def test_declared_capacity_takes_the_durable_fallback(
    scratch_bases: tuple[Path, Path],
) -> None:
    """A caller that declares its footprint (the restore drills) gets the disk
    fallback even when the platform default clears the estimate — the estimate is
    a floor, not a guarantee (2026-10-05: a 15.4 GiB estimate cleared /dev/shm's
    16 GiB; the actual restore did not, and the postmaster died mid-restore)."""
    default, fallback = scratch_bases
    free = {default: 1000, fallback: 1000}
    assert (
        pg_plan.choose_throwaway_base(
            500, override=None, default=default, fallback=fallback, free_space=free
        )[0]
        == fallback
    )
    assert (
        pg_plan.choose_throwaway_base(
            1000, override=None, default=default, fallback=fallback, free_space=free
        )[0]
        == fallback
    )  # >= is enough
    large_free = {default: 10**9, fallback: 10**9}
    assert (
        pg_plan.choose_throwaway_base(
            1, override=None, default=default, fallback=fallback, free_space=large_free
        )[0]
        == fallback
    )


def test_a_default_that_clears_the_estimate_is_not_a_drill_base(
    scratch_bases: tuple[Path, Path],
) -> None:
    """No gamble: when the durable base cannot hold the declared footprint, the
    selection refuses — it never falls back to the RAM-sized default that merely
    clears the same estimate."""
    default, fallback = scratch_bases
    with pytest.raises(base.InsufficientThrowawaySpaceError):
        pg_plan.choose_throwaway_base(
            500,
            override=None,
            default=default,
            fallback=fallback,
            free_space={default: 10**9, fallback: 100},
        )


def test_shortage_everywhere_fails_loudly_naming_each_base(
    scratch_bases: tuple[Path, Path],
) -> None:
    default, fallback = scratch_bases
    with pytest.raises(base.InsufficientThrowawaySpaceError) as excinfo:
        pg_plan.choose_throwaway_base(
            500,
            override=None,
            default=default,
            fallback=fallback,
            free_space={default: 100, fallback: 200},
        )
    message = str(excinfo.value)
    assert str(default) in message and str(fallback) in message
    assert "MEIVA_PG_THROWAWAY_BASE" in message  # the escape hatch is named


def test_a_single_base_serves_or_the_selection_refuses(
    scratch_bases: tuple[Path, Path],
) -> None:
    """Hosts where the disk fallback resolves to the platform default (no /var/tmp,
    the OS temp dir): one candidate — it serves a declared capacity when it
    can hold it, and a shortage refuses instead of returning a base that cannot."""
    default, _fallback = scratch_bases
    assert (
        pg_plan.choose_throwaway_base(
            500,
            override=None,
            default=default,
            fallback=default,
            free_space={default: 1000},
        )[0]
        == default
    )
    with pytest.raises(base.InsufficientThrowawaySpaceError):
        pg_plan.choose_throwaway_base(
            500,
            override=None,
            default=default,
            fallback=default,
            free_space={default: 100},
        )


def test_configured_override_wins_even_when_short(
    scratch_bases: tuple[Path, Path], tmp_path: Path
) -> None:
    """An explicit MEIVA_PG_THROWAWAY_BASE is the operator's call: it is used as-is
    (with a warning), never silently replaced by a demotion."""
    default, fallback = scratch_bases
    forced = tmp_path / "forced"
    forced.mkdir()
    chosen, warning = pg_plan.choose_throwaway_base(
        500,
        override=forced,
        default=default,
        fallback=fallback,
        free_space={forced: 100},
    )
    assert chosen == forced
    assert warning is not None
    assert base.select_throwaway_base(override=forced, default_root=default) == forced


def test_configured_override_must_be_a_real_directory(
    scratch_bases: tuple[Path, Path], tmp_path: Path
) -> None:
    default, _fallback = scratch_bases
    with pytest.raises(RuntimeError, match="MEIVA_PG_THROWAWAY_BASE"):
        base.select_throwaway_base(override=tmp_path / "missing", default_root=default)


def test_throwaway_roots_covers_override_default_and_fallback(
    scratch_bases: tuple[Path, Path], tmp_path: Path
) -> None:
    """The sweep and the port registry enumerate the whole set; equal bases collapse
    so no root is scanned (or locked) twice."""
    default, fallback = scratch_bases
    assert pg_plan.dedupe_throwaway_roots(None, default, fallback) == (default, fallback)
    forced = tmp_path / "forced"
    forced.mkdir()
    assert pg_plan.dedupe_throwaway_roots(forced, default, fallback) == (
        forced,
        default,
        fallback,
    )
    assert pg_plan.dedupe_throwaway_roots(forced, default, default) == (forced, default)
    assert base.throwaway_roots(forced, default_root=default) == (forced, default)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", None),
        (" \t\n", None),
        (" relative/path ", Path("relative/path")),
        (" /scratch/clusters ", Path("/scratch/clusters")),
    ],
)
def test_configured_base_only_strips_and_parses(raw: str, expected: Path | None) -> None:
    assert base.configured_base(raw) == expected


def test_format_bytes_reads_at_both_scales() -> None:
    assert base.format_bytes(2**30) == "1.0 GiB"
    assert base.format_bytes(int(1.5 * 2**30)) == "1.5 GiB"
    assert base.format_bytes(2**20) == "1 MiB"


@pytest.mark.parametrize("explicit_base", [False, True])
def test_throwaway_postgres_creates_the_instance_under_the_given_base(
    postgres_access: None,
    explicit_base: bool,
) -> None:
    """`base=` is where the instance dir actually lands, and teardown removes it
    again — the drill's chosen base is the base used. A short `/tmp` root: the
    Postgres socket path (`<dir>/.s.PGSQL.<port>`) is capped at 103 bytes, so a
    pytest `tmp_path` is too deep for a real cluster."""
    instances = Path(tempfile.mkdtemp(prefix="meiva-base-", dir="/tmp"))
    config = ThrowawayConfig(instances, home=meiva_home())
    try:
        abandoned = instances / "meiva-pg-abandoned"
        abandoned.mkdir()
        (abandoned / pg_tools.OWNER_LOCK_NAME).write_text("{}")
        with pg_tools.throwaway_postgres(
            base=instances if explicit_base else None, config=config
        ) as url:
            assert not abandoned.exists()
            [instance] = list(instances.glob(f"{pg_tools.THROWAWAY_PREFIX}*"))
            assert (instance / "data" / "PG_VERSION").is_file()
            assert pg_tools.resolved_throwaway_dir(instance, config=config) == instance.resolve()
            assert pg_tools.sweep_orphaned_throwaway_clusters(config=config) == 0
            with psycopg.connect(url) as conn:
                assert conn.execute("select 1").fetchone() == (1,)
        assert list(instances.iterdir()) == []
    finally:
        shutil.rmtree(instances, ignore_errors=True)
