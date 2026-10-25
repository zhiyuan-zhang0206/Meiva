# meiva:note
# The content owner is tested as values; the writer's real-file and fault
# contracts stay in base/tests/config/test_dotenv_file.py.

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from base.host.env.dotenv_content import (
    env_line_export_prefix,
    env_line_key,
    env_values_from_text,
    render_env_removal,
    render_env_upsert,
)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("KEY=1", "KEY"),
        ("KEY = 1", "KEY"),
        ("  KEY=1", "KEY"),
        ("export KEY=1", "KEY"),
        ("export  KEY=1", "KEY"),
        ("export\tKEY=1", "KEY"),
        ("\texport KEY = 1", "KEY"),
        ("export 'KEY'=7", "KEY"),
        ("'KEY'=1", "KEY"),
        ('"KEY"=1', '"KEY"'),
        ("exportKEY=1", "exportKEY"),
        ("export=1", "export"),
        ("exportED_KEY=1", "exportED_KEY"),
        ("KEY=1 # note", "KEY"),
        ('KEY="18133', "KEY"),
        ("# KEY=1", None),
        ("", None),
        ("KEY", None),
        ("KEY # note", None),
        ("EXPORT KEY=1", None),
        ("export KEY # note", None),
        ("''=1", None),
        ("'KEY=1", None),
    ],
)
def test_env_line_key_follows_the_parser_grammar(line: str, expected: str | None) -> None:
    assert env_line_key(line) == expected


def test_env_line_key_agrees_with_dotenv_on_assignments() -> None:
    from io import StringIO

    from dotenv import dotenv_values

    for line in (
        "KEY=1",
        "KEY = 1",
        "export KEY=1",
        'export  KEY = "2"',
        "export\tKEY=6",
        "export 'KEY'=5",
        "'KEY'=3",
        '"KEY"=4',
        "exportKEY=7",
        "export=8",
        "KEY= # c",
    ):
        parsed = list(dotenv_values(stream=StringIO(line), interpolate=False))
        assert env_line_key(line) == parsed[0], line


def test_env_line_export_prefix_flags_only_real_export_prefixed_lines() -> None:
    assert env_line_export_prefix("export KEY=1") == "export "
    assert env_line_export_prefix("export\tKEY=1") == "export "
    assert env_line_export_prefix("  export  KEY=1") == "export "
    assert env_line_export_prefix("exportKEY=1") == ""
    assert env_line_export_prefix("export=1") == ""
    assert env_line_export_prefix("KEY=1") == ""


def test_upsert_keeps_later_duplicates_and_appends_in_update_order() -> None:
    lines = ["# comment", "export\tA=first", "A=second", "", "KEEP=3"]
    updates = {"Z": "9", "A": "new", "B": "2"}

    assert render_env_upsert(lines, updates) == (
        b"# comment\nexport A=new\nA=second\n\nKEEP=3\nZ=9\nB=2\n"
    )
    assert lines == ["# comment", "export\tA=first", "A=second", "", "KEEP=3"]
    assert updates == {"Z": "9", "A": "new", "B": "2"}


@pytest.mark.parametrize(
    ("lines", "updates", "expected"),
    [
        ([], {}, b"\n"),
        (["# comment", ""], {}, b"# comment\n\n"),
        (['A="1"', "B = 2"], {"A": "1", "B": "2"}, b"A=1\nB=2\n"),
        (["A", "A # bare", "'A'=old"], {"A": "new"}, b"A\nA # bare\nA=new\n"),
        (["exportKEY=1"], {"KEY": "2"}, b"exportKEY=1\nKEY=2\n"),
    ],
)
def test_upsert_renders_existing_empty_quoted_and_bare_line_behavior(
    lines: list[str], updates: dict[str, str], expected: bytes
) -> None:
    assert render_env_upsert(lines, updates) == expected


@pytest.mark.parametrize(
    ("lines", "keys", "expected"),
    [
        ([], {"A"}, None),
        (["A=1"], set(), None),
        (["A", "# A=1", "KEEP=1"], {"A"}, None),
        (["A=1", "export A=2", "'A'=3"], {"A"}, b"\n"),
        (["A=1", "KEEP=2", "A=3", ""], {"A"}, b"KEEP=2\n\n"),
    ],
)
def test_remove_distinguishes_no_match_from_removing_every_line(
    lines: list[str], keys: set[str], expected: bytes | None
) -> None:
    assert render_env_removal(lines, keys) == expected


def test_audit_value_projection_keeps_its_existing_text_semantics() -> None:
    assert env_values_from_text('A=first\nexport A=\'last\'\nBARE\nB= "two" \nBROKEN="open\n') == {
        "A": "last",
        "B": "two",
        "BROKEN": '"open',
    }


# meiva:note
# A fresh interpreter proves the content door does not pull in the writer or
# configuration owners that happen to be loaded by the surrounding pytest run.
def test_content_import_does_not_load_file_or_configuration_resources(tmp_path: Path) -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-I", str(Path(__file__).with_name("_dotenv_content_child.py"))],
        cwd=tmp_path,
        env={
            **os.environ,
            "MEIVA_HOME": str(tmp_path / "absent-home"),
            "MEIVA_CONFIG_FETCH": "skip",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["source"] == str(
        Path(__file__).parents[1] / "dotenv_content.py"
    )
    assert not (tmp_path / "absent-home").exists()
