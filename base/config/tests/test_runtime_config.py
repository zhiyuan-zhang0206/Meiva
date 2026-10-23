# meiva:note
# `$MEIVA_HOME/.env` is the single config source of truth.
#
# `base/config/runtime_config.py` reads/writes `.env` by each field's env alias.
#
# Every test passes the `.env` path of a `tmp_path` home to the writer and the reader.


from __future__ import annotations

from pathlib import Path

import pytest

from base.config import runtime_config as rt
from base.host.env import dotenv_file


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A fresh unit home; its `.env` is `home / ".env"`."""
    return tmp_path


def _write(home: Path, updates: dict[str, object], removals: set[str]) -> None:
    rt.write_fields(updates, removals, path=dotenv_file.env_file_path(home))


def _aliases(home: Path) -> dict[str, str]:
    return dotenv_file.read_env_aliases(dotenv_file.env_file_path(home))


def _seed_env(home: Path, lines: dict[str, str]) -> None:
    home.joinpath(".env").write_text("".join(f"{k}={v}\n" for k, v in lines.items()))


# ─── .env read / write round-trip ───


class TestEnvRoundtrip:
    def test_write_then_read_alias(self, home: Path) -> None:
        _write(home, {"llm_model": "m1"}, set())
        assert _aliases(home)["MEIVA_MODEL"] == "m1"

    def test_write_fields_pins_env_file_0600(self, home: Path) -> None:
        """.env is the only on-disk copy of a cluster's secrets — a fresh write
        must land 0600 (the umask default would be 0644, readable by every user
        on the box), matching the 0600 of its own backups (snapshot_env)."""
        _write(home, {"anthropic_api_key": "sk-ant-abc"}, set())
        env = home / ".env"
        assert oct(env.stat().st_mode)[-3:] == "600"

        # Idempotent: a later write keeps 0600.
        _write(home, {"anthropic_api_key": "sk-ant-def"}, set())
        assert oct(env.stat().st_mode)[-3:] == "600"

    def test_write_fields_fixes_pre_existing_0644_env(self, home: Path) -> None:
        """An old 0644 .env is repaired by the next write (the chmod runs on
        every write, not just file creation)."""
        env = home / ".env"
        env.write_text("MEIVA_MODEL=m1\n")
        env.chmod(0o644)
        _write(home, {"llm_model": "m2"}, set())
        assert oct(env.stat().st_mode)[-3:] == "600"

    def test_env_set_field_names_reflects_file(self, home: Path) -> None:
        _write(home, {"llm_model": "m1"}, set())
        names = rt.env_set_field_names(dotenv_file.env_file_path(home))
        assert "llm_model" in names
        assert "ops_concurrency" not in names

    def test_bool_stringified_lowercase(self, home: Path) -> None:
        _write(home, {"browser_enabled": True}, set())
        assert _aliases(home)["MEIVA_BROWSER_ENABLED"] == "true"

    def test_removal_unsets_only_that_key(self, home: Path) -> None:
        _write(home, {"llm_model": "m1", "deepseek_api_key": "sk-x"}, set())
        _write(home, {}, {"deepseek_api_key"})
        aliases = _aliases(home)
        assert "DEEPSEEK_API_KEY" not in aliases
        assert aliases["MEIVA_MODEL"] == "m1"  # untouched

    def test_absent_env_reads_empty(self, home: Path) -> None:
        assert _aliases(home) == {}
        assert rt.env_set_field_names(dotenv_file.env_file_path(home)) == set()

    def test_write_fields_snapshots_prior_state(self, home: Path) -> None:
        """Every write snapshots the .env first, so a bad write is recoverable —
        the pre-write value survives in backups even after it's overwritten."""
        _write(home, {"deepseek_api_key": "sk-orig"}, set())
        _write(home, {"deepseek_api_key": "sk-new"}, set())
        backups = sorted((home / "backups" / "env").glob(".env.*"))
        assert any("sk-orig" in b.read_text() for b in backups)
        assert _aliases(home)["DEEPSEEK_API_KEY"] == "sk-new"

    def test_write_preserves_unrelated_lines(self, home: Path) -> None:
        _seed_env(home, {"MEIVA_DB_URL": "postgresql://x@127.0.0.1:1/x"})
        _write(home, {"llm_model": "m1"}, set())
        aliases = _aliases(home)
        assert aliases["MEIVA_DB_URL"] == "postgresql://x@127.0.0.1:1/x"
        assert aliases["MEIVA_MODEL"] == "m1"
