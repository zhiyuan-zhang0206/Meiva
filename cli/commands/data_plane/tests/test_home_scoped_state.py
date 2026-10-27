from __future__ import annotations

from pathlib import Path

from base.cluster.dataplane import runtime_binaries
from base.config import settings
from base.paths import meiva_home
from base.sessions import coding_session_owner_record
from base.sessions.pty import allocation_freeze
from cli.commands.data_plane import cluster_instance


def test_every_host_state_path_follows_meiva_home(unit_home: Path) -> None:
    assert runtime_binaries.runtime_root(meiva_home()) == unit_home / "runtime"
    assert cluster_instance.pg_template_dir(unit_home) == unit_home / "pg-template-17"
    key = coding_session_owner_record.canonical_key(unit_home, tool="codex", cluster=unit_home)
    assert coding_session_owner_record.host_owner_dir(key) == unit_home / "coding-session-owners"
    assert allocation_freeze.state_path(meiva_home()) == unit_home / "pty-allocation-freeze.json"
    assert allocation_freeze.lock_path(meiva_home()) == unit_home / "pty-allocation.lock"


def test_there_is_no_host_state_dir_setting_or_helper() -> None:
    import base.paths

    assert "host_state_dir" not in type(settings.general).model_fields
    assert not hasattr(base.paths, "host_state_dir")
