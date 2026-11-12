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
    w(root, "good.md", fm(description="d"))
    w(
        root,
        "titled.md",
        fm(description="d"),
    )
    w(root, "dup.md", fm(description="d"))
    w(
        root,
        "orphan.md",
        fm(description="d"),
    )
    w(root, "nofm.md", "no frontmatter\n")
    w(root, "unclosed.md", "---\ntype: a\n")
    w(root, "badyaml.md", "---\n: : :\n  - [\n---\nx\n")
    w(root, "notmap.md", "---\n- a\n- b\n---\nx\n")
    w(root, "notype.md", "---\ndescription: d\n---\nx\n")
    w(
        root,
        "nulls.md",
        '---\ndescription: ""\n---\nx\n',
    )
    w(
        root,
        "ts.md",
        fm(description="d"),
    )
    w(
        root,
        "ts2.md",
        fm(description="d"),
    )
    w(
        root,
        "gen.md",
        fm(description="d"),
    )
    w(
        root,
        "gen2.md",
        "---\ndescription: d\n---\n",
    )
    w(
        root,
        "misread.md",
        "---\ndescription: has # hash\n---\n",
    )
    w(root, "badtype.md", fm(description="d"))
    w(
        root,
        "projects/alpha/a.md",
        fm(description="d"),
    )
    w(
        root,
        "misplaced.md",
        fm(description="d"),
    )
    w(
        root,
        "both.md",
        fm(description="d"),
    )
    w(
        root,
        "projects/beta/b.md",
        fm(description="d"),
    )
    w(
        root,
        "pointed/index.md",
        "- [Real Title](a.md)\n- [Wrong](b.md)\n- [dup](a.md)\n- [missing](zzz.md)\n- [d](sub/index.md)\n",
    )
    w(
        root,
        "pointed/a.md",
        fm(description="d"),
    )
    w(root, "pointed/b.md", fm(description="d"))
    w(root, "pointed/c.md", fm(description="d"))
    w(root, "noindex/n.md", fm(description="d"))
    w(root, "fmindex/index.md", "---\ndescription: d\n---\n- [n](n.md)\n")
    w(root, "fmindex/n.md", fm(description="d"))
    w(root, ".hidden/h.md", fm(description="d"))
    w(
        root,
        "fc.md",
        "<!-- fact-check: test -d /nonexistent_x -->\n<!-- fact-check: ! test -d / -->\n<!-- fact-check: rm -rf x -->\n<!-- fact-check: test -d / -->\n",
    )
    for i in range(22):
        w(root, f"many/n{i}.md", fm(description="d"))
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
        "MEMORY.md": ["Line 5: missing_path: gone.md", "Line 6: malformed_source: https://x.md"],
        "pointed/index.md": ["Line 4: missing_path: zzz.md", "Line 5: missing_path: sub/index.md"],
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
        "MEMORY.md: pointer target invalid: gone.md",
        "MEMORY.md: pointer target invalid: https://x.md",
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


_GOOD = "---\ndescription: What it holds\n---\n# A rule\n\nBody.\n"


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
            _note(
                tmp_path, "---\ndescription: Optional frontmatter only\n---\n# Heading\n", "opt.md"
            )
        )
        == []
    )
    assert (
        validator.validate_note(
            _note(
                tmp_path,
                '---\ndescription: "Rule summary"\n'
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
            [
                "Frontmatter key 'sources' is forbidden; allowed keys: description, verified, stale_after"
            ],
        ),
        (
            '---\nsources:\n  - "repo:meiva:a.py"\n  - "  "\n---\n# Heading\n',
            [
                "Frontmatter key 'sources' is forbidden; allowed keys: description, verified, stale_after"
            ],
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
        for name in ("MEMORY.md", "INHERIT.md", "index.md", "log.md", "AGENTS.md", "x.txt")
    )
    parts = validator.note_parts(_GOOD)
    assert parts is not None
    frontmatter, body = parts
    assert frontmatter["description"] == "What it holds" and body == "# A rule\n\nBody.\n"
    assert validator.note_parts("no frontmatter") == ({}, "no frontmatter")
    assert validator.note_parts("---\nunclosed\n") is None
    assert validator.note_parts("---\n: : :\n  - [\n---\nx\n") is None


def test_inline_sources_resolve_files_and_skip_code(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    note = vault / "memory" / "ops" / "note.md"
    note.parent.mkdir(parents=True)
    evidence = vault / "sources" / "ruling.jsonl"
    evidence.parent.mkdir()
    evidence.write_text("{}")
    sibling = note.with_name("sibling.md")
    sibling.write_text("# Evidence")
    text = (
        "# Rule\n[Evidence](/sources/ruling.jsonl#L1)\n"
        "[Sibling](./sibling.md)\n`[ignored](https://example.com)`\n"
        "```md\n[ignored](repo:bad)\n```\n"
    )
    note.write_text(text)
    assert validate.body_links(text) == [(2, "/sources/ruling.jsonl#L1"), (3, "./sibling.md")]
    assert validate.validate_file(note, vault=vault) == []
    assert validate.resolve_link(note, vault, "/sources/ruling.jsonl") == (evidence, vault)
    for target, reason in [
        ("/sources", "not_a_file"),
        ("/missing", "missing_path"),
        ("../../../escape", "malformed_source"),
        ("env:HOME", "malformed_source"),
    ]:
        with pytest.raises(ValueError, match=reason):
            validate.resolve_link(note, vault, target)
    repo = tmp_path / "code"
    repo.mkdir()
    code = repo / "mod.py"
    code.write_text("x=1")
    assert validate.resolve_link(note, vault, "/repos/meiva/mod.py", {"meiva": repo}) == (
        code,
        repo,
    )
    with pytest.raises(ValueError, match="malformed_source"):
        validate.resolve_link(note, vault, "/repos/meiva/../outside", {"meiva": repo})


def test_validator_scans_memory_tree_only(tmp_path: Path) -> None:
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "rule.md").write_text("# Rule")
    (tmp_path / "README.md").write_text("[External](https://example.com)")
    assert validate.validate_bundle(tmp_path) == ([], 1)


@pytest.mark.parametrize("value", ["null", "[]", "[a]"])
def test_sources_frontmatter_always_forbidden(tmp_path: Path, value: str) -> None:
    note = _note(tmp_path, f"---\nsources: {value}\n---\n# Rule")
    assert validate.validate_file(note) == [
        "Frontmatter key 'sources' is forbidden; allowed keys: description, verified, stale_after"
    ]


def test_markdown_link_parser_handles_nested_targets_and_tick_runs() -> None:
    text = (
        '[nested](/sources/a(b(c)).pdf "Title")\n'
        "``code ` [hidden](env:HOME) `` [visible](./note.md)\n"
        "~~~md\n[hidden](http://example.com)\n~~~\n[empty]()\n"
    )
    assert validate.body_links(text) == [(1, "/sources/a(b(c)).pdf"), (2, "./note.md"), (6, "")]


def test_repo_mapping_prefers_explicit_then_vault_then_home(tmp_path: Path) -> None:
    home = tmp_path / ".meiva"
    vault = home / "vault"
    mirror = vault / "repos" / "meiva"
    mirror.mkdir(parents=True)
    source = home / "source"
    source.mkdir()
    assert validate.source_repos(vault)["meiva"] == mirror
    assert validate.source_repos(vault, {"meiva": source})["meiva"] == source
    mirror.rmdir()
    assert validate.source_repos(vault)["meiva"] == source


@pytest.mark.parametrize(
    "target", ["/memory/x.md", "./x.md", "nested/../x.md", "/memory/x.md#claim"]
)
def test_pointer_resolution_normalizes_note_paths(tmp_path: Path, target: str) -> None:
    bundle = tmp_path / "memory"
    w(bundle, "x.md", "# Claim\n")
    (bundle / "nested").mkdir()
    w(bundle, "MEMORY.md", f"[Claim]({target})\n")
    assert validate.validate_pointers(bundle) == []


def test_pointer_duplicates_share_the_resolved_note_identity(tmp_path: Path) -> None:
    bundle = tmp_path / "memory"
    w(bundle, "x.md", "# Claim\n")
    w(bundle, "MEMORY.md", "[Relative](./x.md)\n[Absolute](/memory/x.md#claim)\n")
    assert validate.validate_pointers(bundle) == ["MEMORY.md: duplicate pointer x.md (2x)"]


@pytest.mark.parametrize("target", ["/memory/gone.md", "./gone.md", "/memory/directory.md"])
def test_pointer_invalid_targets_share_the_live_validator_message(
    tmp_path: Path, target: str
) -> None:
    bundle = tmp_path / "memory"
    w(bundle, "MEMORY.md", f"[Missing]({target})\n")
    (bundle / "directory.md").mkdir()
    assert validate.validate_pointers(bundle) == [f"MEMORY.md: pointer target invalid: {target}"]


def test_evidence_links_do_not_cover_notes(tmp_path: Path) -> None:
    bundle = tmp_path / "memory"
    w(tmp_path, "sources/x.md", "# Evidence\n")
    w(bundle, "x.md", "# Claim\n")
    w(bundle, "MEMORY.md", "[Evidence](/sources/x.md)\n")
    assert validate.validate_pointers(bundle) == ["orphan note (no pointer in MEMORY.md): x.md"]


@pytest.mark.parametrize(
    "key",
    [
        "type",
        "name",
        "title",
        "tags",
        "timestamp",
        "meiva_agent",
        "meiva_machine",
        "ava_agent",
        "ava_machine",
        "sources",
        "generated",
        "unknown",
        "42",
    ],
)
def test_unknown_frontmatter_keys_are_rejected(tmp_path: Path, key: str) -> None:
    errors = validate.validate_file(_note(tmp_path, f"---\n{key}: value\n---\n# Rule\n"))
    parsed_key = 42 if key == "42" else key
    assert errors == [
        f"Frontmatter key {parsed_key!r} is forbidden; allowed keys: description, verified, stale_after"
    ]


@pytest.mark.parametrize(
    "comment", ["<!-- agent-7 -->", "<!--ava-agent: 3-->", "<!--  ava-machine: box\n-->"]
)
def test_legacy_attribution_comments_are_rejected(tmp_path: Path, comment: str) -> None:
    assert validate.validate_file(_note(tmp_path, f"# Rule\n{comment}\n")) == [
        "Legacy HTML attribution comments are forbidden"
    ]


def test_other_html_comments_remain_valid(tmp_path: Path) -> None:
    assert validate.validate_file(_note(tmp_path, "# Rule\n<!-- fact-check: test -d / -->\n")) == []


# meiva:note
# The standalone producer keeps the pool's existing scope: MEMORY.md counts toward directory
# shape but is reserved from note length. The report reads actual limits, never adds a gate.
def test_budget_snapshot_uses_pool_limits_and_changed_paths(tmp_path: Path) -> None:
    w(tmp_path, "memory/MEMORY.md", "index")
    w(tmp_path, "memory/near.md", "x" * 7999)
    w(tmp_path, "memory/over.md", "x" * 8001)
    w(tmp_path, "memory/index.md", "ignored in directory count")
    (tmp_path / "memory/child").mkdir()
    snapshot = validate.budget_snapshot(tmp_path, ["./memory/near.md"])
    rows = snapshot["rows"]
    assert isinstance(rows, list)
    assert {row["kind"] for row in rows} == {
        "memory-note-characters",
        "memory-subdirectories",
        "memory-markdown-files",
    }
    by_key = {(row["kind"], row["location"]): row for row in rows}
    directory = by_key[("memory-markdown-files", str(tmp_path / "memory"))]
    assert (directory["current"], directory["limit"], directory["changed"]) == (3, 20, True)
    near = by_key[("memory-note-characters", str(tmp_path / "memory/near.md"))]
    assert (near["current"], near["limit"], near["changed"]) == (
        7999,
        validate.note_char_cap(),
        True,
    )
    assert not by_key[("memory-note-characters", str(tmp_path / "memory/over.md"))]["changed"]
    assert ("memory-note-characters", str(tmp_path / "memory/MEMORY.md")) not in by_key


def test_budget_snapshot_missing_pool_is_not_reported_as_zero(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="memory directory missing"):
        validate.budget_snapshot(tmp_path, [])


def test_report_cli_returns_measurements_without_executing_validation(tmp_path: Path) -> None:
    w(tmp_path, "memory/over.md", "x" * 8001)
    result = run_bounded(
        [
            sys.executable,
            "-I",
            validate.__file__,
            "--budget-report",
            "--bundle",
            str(tmp_path),
            "--changed",
            "memory/over.md",
        ],
        env={"PATH": os.defpath, "MEIVA_MEMORY_NOTE_CAP": "50"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    snapshot = json.loads(result.stdout)
    assert snapshot["schema"] == "meizel.budgets/v1"
    [note] = [row for row in snapshot["rows"] if row["kind"] == "memory-note-characters"]
    assert (note["current"], note["limit"], note["changed"]) == (8001, 50, True)


@pytest.mark.parametrize("name", ["/outside.md", "../outside.md"])
def test_budget_changed_paths_cannot_claim_another_vault(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="relative to the Vault"):
        validate.budget_snapshot(tmp_path, [name])
