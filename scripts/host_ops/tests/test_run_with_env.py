# meiva:note
# Unit tests for the run-with-env entry: the provider parsing, the env it builds, and the refusals.
from __future__ import annotations

from pathlib import Path

from scripts.host_ops.run_with_env import DB_ENV, REDIS_ENV, child_env, main, parse_providers


# meiva:note
# A caller names what it wants; an unknown word is a usage error rather than a silently ignored one,
# because a command that ran without the value it asked for reports a confusing tool error instead.
def test_parse_providers_accepts_the_known_words() -> None:
    assert parse_providers("db,redis") == ["db", "redis"]
    assert parse_providers(" redis ") == ["redis"]


def test_parse_providers_refuses_an_unknown_word() -> None:
    assert main(["--provide", "databse", "--", "/bin/true"]) == 2
    assert main(["--provide", "", "--", "/bin/true"]) == 2


# meiva:note
# Both values are read at call time, so an inherited copy from an older generation must lose.
def test_child_env_overwrites_inherited_values() -> None:
    child = child_env({"MEIVA_DB_URL": "stale", "PATH": "/bin"}, {DB_ENV: "fresh", REDIS_ENV: "r"})
    assert child[DB_ENV] == "fresh"
    assert child[REDIS_ENV] == "r"
    assert child["PATH"] == "/bin"


def test_child_env_leaves_the_parent_mapping_untouched() -> None:
    parent = {"PATH": "/bin"}
    child_env(parent, {DB_ENV: "fresh"})
    assert parent == {"PATH": "/bin"}


# meiva:note
# A home with no installed capability must not hand the child an empty URL: the entry exits
# non-zero instead. `consume_unit` refuses a home outside the admitted runtime through the same
# branch, so this covers both "no capability" and "not this home's runtime".
def test_db_provider_refuses_a_bare_home(tmp_path: Path) -> None:
    assert main(["--provide", "db", "--home", str(tmp_path), "--", "/bin/true"]) == 2


# meiva:note
# The cache URL is a `.env` line; a home without that line refuses rather than passing an empty
# string, and `--check` answers False for it.
def test_redis_provider_refuses_a_home_without_the_line(tmp_path: Path) -> None:
    assert main(["--provide", "redis", "--home", str(tmp_path), "--", "/bin/true"]) == 2
    assert main(["--check", "--provide", "redis", "--home", str(tmp_path)]) == 1


def test_redis_provider_reads_the_line(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("OTHER=1\nMEIVA_REDIS_URL='redis://:pw@h:6379/0'\n")
    assert main(["--check", "--provide", "redis", "--home", str(tmp_path)]) == 0


# meiva:note
# No separator is a usage error, not an empty command: running nothing must never look like a
# successful no-op.
def test_main_refuses_a_missing_separator() -> None:
    assert main(["--provide", "db", "/bin/true"]) == 2
