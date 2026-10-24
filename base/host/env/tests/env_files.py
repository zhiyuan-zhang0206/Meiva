from pathlib import Path

import pytest

from base.host.env.dotenv_file import process_env_file, read_env_aliases

__all__ = ["unit_env_aliases", "unit_env_path", "use_env_files"]


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


def use_env_files(
    monkeypatch: pytest.MonkeyPatch, env_file: Path, mirror_file: Path | None = None
) -> Path:
    home = env_file.parent / f"{env_file.stem}-home"
    home.mkdir(exist_ok=True)
    (home / ".env").write_text(env_file.read_text())
    if mirror_file is not None and mirror_file.exists():
        (home / "mirror.env").write_text(mirror_file.read_text())
    monkeypatch.setenv("MEIVA_HOME", str(home))
    return home
