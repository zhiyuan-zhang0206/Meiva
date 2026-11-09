import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

from base.host.proc import run_bounded
from meiva_builtins.plugins.meiva_memory.template import validate

_CAP_CHILD = Path(__file__).with_name("child_note_cap.py")


def _validator() -> ModuleType:
    return validate


def w(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def fm(**kw: str) -> str:
    lines = ["---"]
    for k, v in kw.items():
        lines.append(f"{k}: {v}")
    lines.append("---\nbody\n")
    return "\n".join(lines)


def build(root: Path) -> None:
    w(
        root,
        "MEMORY.md",
        "- [Good](good.md) — x\n- [Wrong title](titled.md)\n- [dup](dup.md)\n- [dup2](dup.md)\n- [gone](gone.md)\n- [http](https://x.md)\n- [Dirpointed](pointed/index.md)\n",
    )
    w(root, "good.md", fm(type="note", meiva_agent="1", description="d", tags="[type/user]"))
    w(
        root,
        "titled.md",
        fm(
            type="note",
            meiva_agent="1",
            title="Real Title",
            description="d",
            tags="[type/feedback]",
        ),
    )
    w(root, "dup.md", fm(type="note", meiva_agent="1", description="d", tags="[type/env, project]"))
    w(
        root,
        "orphan.md",
        fm(type="note", meiva_agent="1", description="d", tags="[type/role, type/user]"),
    )
    w(root, "nofm.md", "no frontmatter\n")
    w(root, "unclosed.md", "---\ntype: a\n")
    w(root, "badyaml.md", "---\n: : :\n  - [\n---\nx\n")
    w(root, "notmap.md", "---\n- a\n- b\n---\nx\n")
    w(root, "notype.md", "---\nmeiva_agent: 1\ndescription: d\ntags: [a, 1]\n---\nx\n")
    w(
        root,
        "nulls.md",
        '---\ntype: ""\nmeiva_agent: null\ndescription: ""\ntags: notalist\n---\nx\n',
    )
    w(
        root,
        "ts.md",
        fm(
            type="n",
            meiva_agent="1",
            description="d",
            tags="[type/user]",
            timestamp="2026-01-01T00:00:00",
        ),
    )
    w(
        root,
        "ts2.md",
        fm(type="n", meiva_agent="1", description="d", tags="[type/user]", timestamp="bogus"),
    )
    w(
        root,
        "gen.md",
        fm(type="n", meiva_agent="1", description="d", tags="[type/user]", generated="x"),
    )
    w(
        root,
        "gen2.md",
        '---\ntype: n\nmeiva_agent: 1\ndescription: d\ntags: [type/user]\ngenerated:\n  by: ""\n  at: nope\n---\n',
    )
    w(
        root,
        "misread.md",
        "---\ntype: n\nmeiva_agent: 1\ndescription: has # hash\ntags: [type/user]\n---\n",
    )
    w(root, "badtype.md", fm(type="n", meiva_agent="1", description="d", tags="[type/bogus, repo]"))
    w(
        root,
        "projects/alpha/a.md",
        fm(type="n", meiva_agent="1", description="d", tags="[type/project, alpha]"),
    )
    w(
        root,
        "misplaced.md",
        fm(type="n", meiva_agent="1", description="d", tags="[type/project, alpha]"),
    )
    w(
        root,
        "both.md",
        fm(type="n", meiva_agent="1", description="d", tags="[type/project, alpha, beta, meiva]"),
    )
    w(
        root,
        "projects/beta/b.md",
        fm(type="n", meiva_agent="1", description="d", tags="[type/project]"),
    )
    w(
        root,
        "pointed/index.md",
        "- [Real Title](a.md)\n- [Wrong](b.md)\n- [dup](a.md)\n- [missing](zzz.md)\n- [d](sub/index.md)\n",
    )
    w(
        root,
        "pointed/a.md",
        fm(type="n", meiva_agent="1", title="Real Title", description="d", tags="[type/user]"),
    )
    w(root, "pointed/b.md", fm(type="n", meiva_agent="1", description="d", tags="[type/user]"))
    w(root, "pointed/c.md", fm(type="n", meiva_agent="1", description="d", tags="[type/user]"))
    w(root, "noindex/n.md", fm(type="n", meiva_agent="1", description="d", tags="[type/user]"))
    w(root, "fmindex/index.md", "---\nx: 1\n---\n- [n](n.md)\n")
    w(root, "fmindex/n.md", fm(type="n", meiva_agent="1", description="d", tags="[type/user]"))
    w(root, ".hidden/h.md", fm(type="n"))
    w(
        root,
        "fc.md",
        "<!-- fact-check: test -d /nonexistent_x -->\n<!-- fact-check: ! test -d / -->\n<!-- fact-check: rm -rf x -->\n<!-- fact-check: test -d / -->\n",
    )
    for i in range(22):
        w(root, f"many/n{i}.md", fm(type="n", meiva_agent="1", description="d", tags="[type/user]"))
    for i in range(21):
        (root / f"dirs/d{i}").mkdir(parents=True)
    w(root, "unicode.md", "x")
    (root / "bad_utf.md").write_bytes(b"\xff\xfe")


_EXPECTED = {
    "validate_factchecks": [
        "fc.md: fact-check FAILED: !test -d /",
        "fc.md: fact-check FAILED: test -d /nonexistent_x",
        "fc.md: fact-check command not whitelisted: 'rm -rf x' (allowed: ['git', 'ls', 'test'])",
    ],
    "validate_file": {
        "bad_utf.md": ["File is not valid UTF-8"],
        "badyaml.md": [
            "Invalid YAML: while parsing a block mapping\n"
            "expected <block end>, but found ':'\n"
            '  in "<unicode string>", line 1, column 1:\n'
            "    : : :\n"
            "    ^"
        ],
        "misread.md": [
            "Field 'description' is YAML-misread: raw text differs from the "
            "parsed value (an unquoted ' #' or ': ' truncates it) — wrap the "
            "value in double quotes"
        ],
        "notmap.md": ["Frontmatter must be a YAML mapping"],
        "unclosed.md": ["Unclosed YAML frontmatter"],
    },
    "validate_pointers": [
        "MEMORY.md: duplicate pointer dup.md (2x)",
        "MEMORY.md: pointer target missing: gone.md",
        "directory without pointer: .hidden/",
        "directory without pointer: fmindex/",
        "directory without pointer: many/",
        "directory without pointer: noindex/",
        "directory without pointer: projects/",
        "orphan note (no pointer in MEMORY.md): bad_utf.md",
        "orphan note (no pointer in MEMORY.md): badtype.md",
        "orphan note (no pointer in MEMORY.md): badyaml.md",
        "orphan note (no pointer in MEMORY.md): both.md",
        "orphan note (no pointer in MEMORY.md): fc.md",
        "orphan note (no pointer in MEMORY.md): gen.md",
        "orphan note (no pointer in MEMORY.md): gen2.md",
        "orphan note (no pointer in MEMORY.md): misplaced.md",
        "orphan note (no pointer in MEMORY.md): misread.md",
        "orphan note (no pointer in MEMORY.md): nofm.md",
        "orphan note (no pointer in MEMORY.md): notmap.md",
        "orphan note (no pointer in MEMORY.md): notype.md",
        "orphan note (no pointer in MEMORY.md): nulls.md",
        "orphan note (no pointer in MEMORY.md): orphan.md",
        "orphan note (no pointer in MEMORY.md): ts.md",
        "orphan note (no pointer in MEMORY.md): ts2.md",
        "orphan note (no pointer in MEMORY.md): unclosed.md",
        "orphan note (no pointer in MEMORY.md): unicode.md",
    ],
    "validate_structure": [
        "(root): 22 md files, over the 20 cap — split into topical subdirectories",
        "dirs: 21 subdirectories, over the 20 cap — merge or re-group",
        "many: 22 md files, over the 20 cap — split into topical subdirectories",
    ],
}


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("pool")
    build(root)
    return root


@pytest.mark.parametrize(
    "name",
    [
        "validate_structure",
        "validate_factchecks",
        "validate_pointers",
    ],
)
def test_bundle_validator_reports_its_violations(bundle: Path, name: str) -> None:
    validator = _validator()
    check: Callable[[Path], list[str]] = {
        "validate_structure": validator.validate_structure,
        "validate_factchecks": validator.validate_factchecks,
        "validate_pointers": validator.validate_pointers,
    }[name]
    assert sorted(check(bundle)) == _EXPECTED[name]


def test_retired_validators_are_removed() -> None:
    validator = _validator()
    assert not hasattr(validator, "validate_type_tag")
    assert not hasattr(validator, "validate_project_home")
    assert not hasattr(validator, "validate_indexes")


def test_file_validator_reports_each_frontmatter_violation(bundle: Path) -> None:
    validate_file = _validator().validate_file
    found: dict[str, list[str]] = {}
    for note in sorted(bundle.rglob("*.md")):
        errors = validate_file(note)
        if errors:
            found[str(note.relative_to(bundle))] = sorted(errors)
    assert found == _EXPECTED["validate_file"]


def _note(tmp_path: Path, text: str, name: str = "n.md") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


_GOOD = (
    "---\ntype: Memory\nmeiva_agent: 7\nmeiva_machine: box\ntimestamp: '2026-01-31T09:30:00+00:00'\n"
    "title: A rule\ndescription: What it holds\ntags: [type/feedback]\n---\nBody.\n"
)


def test_validate_note_accepts_a_complete_note(tmp_path: Path) -> None:
    validator = _validator()
    assert validator.validate_note(_note(tmp_path, _GOOD)) == []
    assert (
        validator.validate_note(
            _note(tmp_path, "# Pure Markdown\n\nHigh-level description.\n\nBody.\n", "pure.md")
        )
        == []
    )
    assert (
        validator.validate_note(
            _note(tmp_path, "---\ntitle: Optional frontmatter only\n---\n# Heading\n", "opt.md")
        )
        == []
    )
    assert (
        validator.validate_note(
            _note(
                tmp_path,
                '---\ndescription: "Rule summary"\nsources:\n  - "repo:meiva:scripts/hooks/diff_checks.py"\n'
                'verified: "2026-11-09"\nstale_after: "2026-12-01"\n---\n# Rule\n',
                "dream.md",
            )
        )
        == []
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("---\ntype: a\n", ["Unclosed YAML frontmatter"]),
        ("---\n- a\n- b\n---\nx\n", ["Frontmatter must be a YAML mapping"]),
        (
            "---\ndescription: has # hash\n---\n",
            [
                "Field 'description' is YAML-misread: raw text differs from the parsed value "
                "(an unquoted ' #' or ': ' truncates it) — wrap the value in double quotes"
            ],
        ),
        (
            "---\nsources: not-a-list\n---\n# Heading\n",
            ["Field 'sources' must be a list of non-empty strings"],
        ),
        (
            '---\nsources:\n  - "repo:meiva:a.py"\n  - "  "\n---\n# Heading\n',
            ["Field 'sources' must be a list of non-empty strings"],
        ),
        (
            "---\nverified: 2026-11-09\n---\n# Heading\n",
            [
                "Field 'verified' must be a quoted YYYY-MM-DD date string: datetime.date(2026, 11, 9)"
            ],
        ),
        (
            '---\nstale_after: "not-a-date"\n---\n# Heading\n',
            ["Field 'stale_after' must be a quoted YYYY-MM-DD date string: 'not-a-date'"],
        ),
        (
            "---\nverified: 2026-11-09 # trailing comment\n---\n# Heading\n",
            [
                "Field 'verified' must be a quoted YYYY-MM-DD date string: datetime.date(2026, 11, 9)",
            ],
        ),
        (
            "---\nstale_after: soon # comment\n---\n# Heading\n",
            [
                "Field 'stale_after' is YAML-misread: raw text differs from the parsed value "
                "(an unquoted ' #' or ': ' truncates it) — wrap the value in double quotes",
                "Field 'stale_after' must be a quoted YYYY-MM-DD date string: 'soon'",
            ],
        ),
    ],
)
def test_validate_note_reports_each_single_note_rule(
    tmp_path: Path, text: str, expected: list[str]
) -> None:
    assert sorted(_validator().validate_note(_note(tmp_path, text))) == sorted(expected)


def test_validate_note_applies_the_character_cap(tmp_path: Path) -> None:
    validator = _validator()
    cap = validator.note_char_cap()
    assert cap == 8000
    note = _note(tmp_path, _GOOD + "x" * (cap - len(_GOOD) + 1))
    assert validator.validate_note(note) == [f"Note is {cap + 1} characters, over the {cap} cap"]
    exact = _note(tmp_path, _GOOD + "x" * (cap - len(_GOOD)), "exact.md")
    assert validator.validate_note(exact) == []


def test_validate_note_cap_follows_the_hook_variable(tmp_path: Path) -> None:
    note = _note(tmp_path, _GOOD)

    run = run_bounded(
        [sys.executable, "-I", str(_CAP_CHILD), str(note)],
        env={"PATH": os.defpath, "MEIVA_MEMORY_NOTE_CAP": "50"},
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == [50, [f"Note is {len(_GOOD)} characters, over the 50 cap"]]


def test_validate_note_leaves_reserved_and_unreadable_files_to_validate_file(
    tmp_path: Path,
) -> None:
    validator = _validator()
    assert validator.validate_note(_note(tmp_path, "no frontmatter", "MEMORY.md")) == []
    assert validator.validate_note(tmp_path / "gone.md") == [
        f"File not found: {tmp_path / 'gone.md'}"
    ]
    binary = tmp_path / "bin.md"
    binary.write_bytes(b"\xff\xfe")
    assert validator.validate_note(binary) == ["File is not valid UTF-8"]


def test_note_helpers(tmp_path: Path) -> None:
    validator = _validator()
    assert validator.is_note_name("topic.md")
    assert not any(
        validator.is_note_name(name)
        for name in ("MEMORY.md", "index.md", "log.md", "AGENTS.md", "x.txt")
    )
    parts = validator.note_parts(_GOOD)
    assert parts is not None
    frontmatter, body = parts
    assert frontmatter["description"] == "What it holds" and body == "Body.\n"
    assert validator.note_parts("no frontmatter") == ({}, "no frontmatter")
    assert validator.note_parts("---\nunclosed\n") is None
    assert validator.note_parts("---\n: : :\n  - [\n---\nx\n") is None
