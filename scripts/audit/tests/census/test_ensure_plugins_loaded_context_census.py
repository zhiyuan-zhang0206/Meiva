# meiva:note
# The census that keeps the #8062 family closed judges call sites, not lines.
#
# `ensure_plugins_loaded` loads the plugin surface for a process that may have bound no context.
# #8062 made the host-version gate take the environment its `git` runs under from the caller, so
# a load site without one raises on the first name listing, and `install.mark_load_failed()` keeps
# that process failing afterwards. The census separates "names a context", "inherits one this
# file bound earlier" and "has none"; the last must stay empty. Each case builds a tree and reads
# the census over it.

from __future__ import annotations

from pathlib import Path

from scripts.audit import ensure_plugins_loaded_context_census as census

REPOSITORY = Path(__file__).resolve().parents[4]


def _write(root: Path, relative: str, body: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _states(root: Path) -> list[tuple[str, str]]:
    return [(site.path, site.state) for site in census.census(root)]


def test_a_call_under_a_file_that_names_no_context_and_binds_none_is_unbound(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "app/boot.py", "def go(meiva):\n    meiva.ensure_plugins_loaded()\n")

    assert _states(tmp_path) == [("app/boot.py", "UNBOUND")]
    assert census.main([str(tmp_path)]) == 1


def test_a_context_built_without_its_environment_is_unbound(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app/child.py",
        "def go(meiva):\n    meiva.ensure_plugins_loaded(context=MeivaContext(meiva_home=home))\n",
    )

    assert _states(tmp_path) == [("app/child.py", "UNBOUND")]
    assert census.main([str(tmp_path)]) == 1


def test_a_context_that_names_its_environment_is_explicit(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app/child.py",
        "def go(meiva):\n"
        "    meiva.ensure_plugins_loaded(\n"
        "        context=MeivaContext(meiva_home=home, environ=os.environ)\n"
        "    )\n",
    )

    assert _states(tmp_path) == [("app/child.py", "EXPLICIT")]
    assert census.main([str(tmp_path)]) == 0


def test_a_context_handed_in_by_the_caller_is_explicit(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app/boot.py",
        "def go(meiva, context):\n    meiva.ensure_plugins_loaded(context=context)\n",
    )

    assert _states(tmp_path) == [("app/boot.py", "EXPLICIT")]
    assert census.main([str(tmp_path)]) == 0


def test_comments_and_script_strings_are_not_call_sites(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app/doc.py",
        '"""Call `meiva.ensure_plugins_loaded()` yourself."""\n'
        "# meiva.ensure_plugins_loaded()\n"
        'CHILD = "meiva.ensure_plugins_loaded()"\n',
    )

    assert census.census(tmp_path) == []
    assert census.main([str(tmp_path)]) == 0


def test_the_repository_has_no_load_site_without_a_context() -> None:
    sites = census.census(REPOSITORY)

    assert [site.path for site in sites if site.unbound] == []
    assert len(sites) == 30
