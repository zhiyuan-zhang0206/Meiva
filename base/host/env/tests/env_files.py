from collections.abc import Callable
from pathlib import Path

import pytest

from base.host.env.dotenv_file import process_env_file, read_env_aliases

__all__ = [
    "aliases_of",
    "constant",
    "env_path_of",
    "unit_env_aliases",
    "unit_env_path",
    "use_env_files",
]


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


# meiva:note
# Typed stand-ins for the ambient readers a few tests still replace. Each answers any call
# meiva:note
# the production code makes, so a test states the answer and not the call's arguments.


# meiva:note
# A function that returns `value` whatever it is called with.
def constant[T](value: T) -> Callable[..., T]:

    def answer(*_args: object, **_kwargs: object) -> T:
        return value

    return answer


# meiva:note
# A `read_env_aliases` that returns a copy of `values` for any path.
def aliases_of(values: dict[str, str]) -> Callable[[Path], dict[str, str]]:

    def read(_env_path: Path) -> dict[str, str]:
        return dict(values)

    return read


# meiva:note
# An `env_file_path` that answers `path` for any home.
def env_path_of(path: Path) -> Callable[[Path], Path]:

    def locate(_home: Path) -> Path:
        return path

    return locate
