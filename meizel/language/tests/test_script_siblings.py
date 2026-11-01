# meiva:note
# `meizel/language/script_siblings.py` — the decisions of the PYTHONSAFEPATH sibling-import guard,
# judged from values alone: the repository-relative name, the text, and what sits beside the file.

from __future__ import annotations

import textwrap

import pytest

from meizel.language import script_siblings

_SCRIPT = textwrap.dedent(
    """    import helper
    import sys

    def main() -> int:
        return 0

    if __name__ == "__main__":
        sys.exit(main())
    """
)


def test_a_sibling_the_caller_reports_is_flagged() -> None:
    found = script_siblings.violations("tools/runner.py", _SCRIPT, {"helper": "file"})
    assert len(found) == 1
    assert found[0].startswith("tools/runner.py:1: ")
    assert "`helper`" in found[0]
    assert "sys.path.insert" in found[0]


def test_the_same_source_is_silent_when_no_sibling_exists() -> None:
    assert script_siblings.violations("tools/runner.py", _SCRIPT, {}) == []


def test_a_sibling_package_is_flagged_through_its_dotted_import() -> None:
    source = _SCRIPT.replace("import helper", "from helper.sub import VALUE")
    assert len(script_siblings.violations("tools/runner.py", source, {"helper": "pkg"})) == 1


def test_a_sibling_file_is_not_a_package() -> None:
    source = _SCRIPT.replace("import helper", "from helper.sub import VALUE")
    assert script_siblings.violations("tools/runner.py", source, {"helper": "file"}) == []


def test_a_module_only_file_is_not_script_mode() -> None:
    source = "import helper\n\nVALUE = 1\n"
    assert script_siblings.violations("tools/runner.py", source, {"helper": "file"}) == []


def test_a_guard_before_the_import_is_enough() -> None:
    source = (
        "import os\nimport sys\n\n"
        "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n"
        "import helper\n"
    )
    assert script_siblings.violations("tools/runner.py", source, {"helper": "file"}) == []


def test_a_guard_after_the_import_does_not_spare_it() -> None:
    source = textwrap.dedent(
        """\
        import os
        import sys

        import helper

        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

        def main() -> int:
            return 0

        if __name__ == "__main__":
            sys.exit(main())
        """
    )
    assert len(script_siblings.violations("tools/runner.py", source, {"helper": "file"})) == 1


def test_a_type_checking_import_is_never_executed() -> None:
    source = "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    import helper\n"
    assert script_siblings.violations("tools/runner.py", source, {"helper": "file"}) == []


def test_the_repair_names_a_package_under_a_scope_that_refuses_guards() -> None:
    found = script_siblings.violations(
        "meiva_builtins/skills/demo/runner.py",
        _SCRIPT,
        {"helper": "file"},
        scope=("meiva_builtins/",),
    )
    assert len(found) == 1
    assert "governed package" in found[0]
    assert "sys.path.insert" not in found[0]


@pytest.mark.parametrize(
    ("rel", "judged"),
    [
        ("meizel/language/tools/runner.py", True),
        ("meizel/language/tests/test_x.py", False),
        ("scripts/lint/test_thing.py", False),
        ("scripts/lint/thing_test.py", False),
    ],
)
def test_which_paths_the_rule_judges(rel: str, judged: bool) -> None:
    assert script_siblings.judged(rel) is judged


def test_vendored_directories_are_excluded_below_the_root_only() -> None:
    assert script_siblings.excluded(("node_modules", "pkg", "mod.py")) is True
    assert script_siblings.excluded(("tools", "tmp", "mod.py")) is True
    assert script_siblings.excluded(("tools", "mod.py")) is False


# meiva:note
# The rule's own source is the evidence: a rule that opens a path cannot be judged from a value.
def test_the_rule_reads_no_file_of_its_own() -> None:
    source = script_siblings.__file__
    text = open(source, encoding="utf-8").read()  # noqa: PTH123, SIM115
    for forbidden in ("Path(", "open(", "read_text", "read_bytes", "iterdir", "rglob"):
        assert forbidden not in text.replace("its own", "")
