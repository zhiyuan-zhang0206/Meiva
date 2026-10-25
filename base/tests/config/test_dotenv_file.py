import os
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from base.host.env.dotenv_file import (
    ENV_BACKUP_KEEP,
    remove_env,
    snapshot_env,
    upsert_env,
)


def _backups(env_path: Path) -> list[Path]:
    return sorted((env_path.parent / "backups" / "env").glob(".env.*"))


def test_upsert_adds_and_updates(tmp_path: Path):
    f = tmp_path / ".env"
    f.write_text("MEIVA_DB_URL=old\nKEEP=1\n")
    upsert_env(f, {"MEIVA_DB_URL": "new", "MEIVA_CLUSTER": "t1"})
    text = f.read_text()
    assert "MEIVA_DB_URL=new" in text
    assert "MEIVA_CLUSTER=t1" in text
    assert "KEEP=1" in text
    assert text.count("MEIVA_DB_URL=") == 1


def test_upsert_creates_file_and_preserves_comments(tmp_path: Path):
    f = tmp_path / "sub" / ".env"
    upsert_env(f, {"A": "1"})
    assert f.read_text() == "A=1\n"

    f.write_text("# comment\n\nA=1\n")
    upsert_env(f, {"B": "2"})
    text = f.read_text()
    assert "# comment" in text
    assert "" in text.splitlines()
    assert "A=1" in text and "B=2" in text


def test_upsert_writes_0600(tmp_path: Path):
    f = tmp_path / ".env"
    f.write_text("OLD=1\n")
    upsert_env(f, {"NEW": "2"})
    assert oct(f.stat().st_mode)[-3:] == "600"


def test_upsert_creates_new_file_0600(tmp_path: Path):
    f = tmp_path / "sub" / ".env"
    upsert_env(f, {"A": "1"})
    assert oct(f.stat().st_mode)[-3:] == "600"


def test_remove_env_keeps_0600(tmp_path: Path):
    f = tmp_path / ".env"
    f.write_text("KEEP=1\nDROP=2\n")
    f.chmod(0o600)
    remove_env(f, {"DROP"})
    assert oct(f.stat().st_mode)[-3:] == "600"
    assert f.read_text() == "KEEP=1\n"


def test_remove_env_repairs_0644(tmp_path: Path):
    f = tmp_path / ".env"
    f.write_text("A=1\nB=2\n")
    f.chmod(0o644)
    remove_env(f, {"B"})
    assert oct(f.stat().st_mode)[-3:] == "600"


def test_snapshot_absent_or_blank_is_noop(tmp_path: Path):
    env = tmp_path / ".env"
    assert snapshot_env(env) is None
    env.write_text("   \n")
    assert snapshot_env(env) is None
    assert not (tmp_path / "backups").exists()


def test_snapshot_backs_up_content_0600(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("SECRET=abc\n")
    dest = snapshot_env(env)
    assert dest is not None
    assert dest.read_text() == "SECRET=abc\n"
    assert oct(dest.stat().st_mode)[-3:] == "600"


_HOST_ZONE = "Pacific/Kiritimati"
_DECOY_CLUSTER_ZONE = "Etc/GMT+12"


def test_snapshot_filename_stamps_the_host_clock(tmp_path: Path) -> None:
    from base.config import ConfigBoot, apply_cluster_timezone

    original_tz = os.environ.get("TZ")
    try:
        owner = ConfigBoot(environment={"MEIVA_CONFIG_FETCH": "skip", "MEIVA_TIMEZONE": _HOST_ZONE})
        apply_cluster_timezone(owner)
        env = tmp_path / ".env"
        env.write_text("SECRET=abc\n")
        before = datetime.now().astimezone()
        dest = snapshot_env(env)
        after = datetime.now().astimezone()
        decoy_now = datetime.now(ZoneInfo(_DECOY_CLUSTER_ZONE))

        assert dest is not None
        stamp = dest.name[len(".env.") :].split("-")[0]
        assert len(stamp) == 8
        assert stamp in {before.strftime("%Y%m%d"), after.strftime("%Y%m%d")}
        assert stamp != decoy_now.strftime("%Y%m%d")
    finally:
        if original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_tz
        time.tzset()


def test_snapshot_dedupes_identical(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("A=1\n")
    snapshot_env(env)
    assert snapshot_env(env) is None
    assert len(_backups(env)) == 1


def test_snapshot_records_each_distinct_state(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("A=1\n")
    snapshot_env(env)
    env.write_text("A=2\n")
    snapshot_env(env)
    backups = _backups(env)
    assert len(backups) == 2
    assert {b.read_text() for b in backups} == {"A=1\n", "A=2\n"}


def test_snapshot_prunes_to_keep(tmp_path: Path):
    env = tmp_path / ".env"
    backup_dir = tmp_path / "backups" / "env"
    backup_dir.mkdir(parents=True)
    for i in range(ENV_BACKUP_KEEP + 3):
        (backup_dir / f".env.{i:04d}").write_text(f"old-{i}\n")
    env.write_text("NEW=1\n")
    snapshot_env(env)
    remaining = _backups(env)
    assert len(remaining) == ENV_BACKUP_KEEP
    assert remaining[-1].read_text() == "NEW=1\n"


def test_upsert_env_snapshots_before_write(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("SECRET=orig\n")
    upsert_env(env, {"SECRET": "new"})
    assert env.read_text().strip() == "SECRET=new"
    backups = _backups(env)
    assert len(backups) == 1
    assert backups[0].read_text() == "SECRET=orig\n"


def test_upsert_atomically_replaces_the_complete_env(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("SECRET=old\nKEEP=1\n")
    old_inode = env.stat().st_ino

    upsert_env(env, {"SECRET": "new"})

    assert env.read_text() == "SECRET=new\nKEEP=1\n"
    assert env.stat().st_ino != old_inode
    assert list(tmp_path.glob(".*.tmp")) == []


def test_upsert_noop_writes_nothing(tmp_path: Path) -> None:
    from base.host.env import dotenv_file

    f = tmp_path / ".env"
    f.write_text("A=1\nKEEP=2\n")
    before = f.stat()

    class _NoRecord:
        def prepare(self, changes: Sequence[Mapping[str, object]]) -> object:
            raise AssertionError("an unchanged upsert must not prepare an audit record")

        def record(
            self, path: Path, written: set[str], removed: set[str], prepared: object
        ) -> None:
            raise AssertionError("an unchanged upsert must not record an audit entry")

    dotenv_file.upsert_env(f, {"A": "1"}, audit=_NoRecord())

    after = f.stat()
    assert f.read_text() == "A=1\nKEEP=2\n"
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert _backups(f) == []
    assert not (tmp_path / ".env.audit.jsonl").exists()


def test_upsert_noop_still_normalizes_a_quirky_line(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text('A="1"\nB = 2\n')
    upsert_env(f, {"A": "1", "B": "2"})
    assert f.read_text() == "A=1\nB=2\n"


def test_upsert_noop_is_decided_on_the_whole_result(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text("A=1\nB=2\n")
    upsert_env(f, {"A": "1", "B": "3"})
    assert f.read_text() == "A=1\nB=3\n"


def test_upsert_noop_leaves_a_symlinked_env_untouched(tmp_path: Path) -> None:
    target = tmp_path / "real.env"
    target.write_text("A=1\n")
    link = tmp_path / ".env"
    link.symlink_to(target)

    upsert_env(link, {"A": "1"})

    assert link.is_symlink()
    assert link.read_text() == "A=1\n"
    assert target.read_text() == "A=1\n"


def test_upsert_rewrites_an_export_prefixed_line_in_place(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text("export MEIVA_DB_URL=old\nKEEP=1\nexport\tMEIVA_CLUSTER=old2\n")
    upsert_env(f, {"MEIVA_DB_URL": "new", "MEIVA_CLUSTER": "t1"})
    assert f.read_text() == "export MEIVA_DB_URL=new\nKEEP=1\nexport MEIVA_CLUSTER=t1\n"


def test_upsert_leaves_lookalike_export_keys_alone(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text("exportED_KEY=keep\n")
    upsert_env(f, {"MEIVA_DB_URL": "new"})
    assert f.read_text() == "exportED_KEY=keep\nMEIVA_DB_URL=new\n"


def test_remove_env_drops_export_prefixed_lines(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text("export MEIVA_OLD_KEY=6543\nKEEP=1\n")
    remove_env(f, {"MEIVA_OLD_KEY"})
    assert f.read_text() == "KEEP=1\n"


# meiva:note
# Decoded equality is insufficient: a CRLF file must take the real write path
# when the candidate bytes use LF, retaining the previous text in its backup.
def test_upsert_compares_the_candidate_with_raw_crlf_bytes(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_bytes(b"A=1\r\nKEEP=2\r\n")

    upsert_env(env, {"A": "1"})

    assert env.read_bytes() == b"A=1\nKEEP=2\n"
    backups = _backups(env)
    assert len(backups) == 1
    assert backups[0].read_text() == "A=1\nKEEP=2\n"


@pytest.mark.parametrize("original", [b"", b"A=1\r\nKEEP=2"])
def test_remove_without_a_matching_assignment_preserves_raw_bytes(
    tmp_path: Path, original: bytes
) -> None:
    env = tmp_path / ".env"
    env.write_bytes(original)

    remove_env(env, {"ABSENT"})

    assert env.read_bytes() == original
    assert _backups(env) == []


def test_remove_missing_file_does_not_create_a_lock_or_home(tmp_path: Path) -> None:
    home = tmp_path / "absent-home"
    remove_env(home / ".env", {"A"})
    assert not home.exists()


def test_removing_every_assignment_writes_one_newline(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("A=1\nA=2\n")

    remove_env(env, {"A"})

    assert env.read_bytes() == b"\n"
    assert _backups(env)[0].read_text() == "A=1\nA=2\n"


@pytest.mark.parametrize("exists", [False, True])
def test_empty_upsert_still_renders_a_newline(tmp_path: Path, exists: bool) -> None:
    env = tmp_path / ".env"
    if exists:
        env.write_bytes(b"")

    upsert_env(env, {})

    assert env.read_bytes() == b"\n"
