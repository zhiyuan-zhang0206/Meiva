#!/usr/bin/env python3
# meiva:link docs/conventions/changelog.md
from __future__ import annotations

import argparse
import difflib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = ["fold_fragments", "main", "parse_changelog", "read_fragment", "render_changelog"]

REPO_ROOT = Path(__file__).resolve().parents[2]

FRAGMENT_DIR = ".changes"
CHANGELOG = "CHANGELOG.md"
UNRELEASED = "## [Unreleased]"

# meiva:note
# Keep a Changelog's order, which CHANGELOG.md already uses; the fold emits the
# sections in this order and rejects any other heading.
SECTIONS = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security")

_TYPE_LINE = re.compile(r"type: (\w+)")
_FRAGMENT_NAME = re.compile(r"(\d+)-([a-z0-9][a-z0-9-]*)\.md")


class ChangelogError(ValueError):
    pass


@dataclass(frozen=True)
class Fragment:
    path: Path
    section: str
    entry: str


def _parse_sections(body: list[str]) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    section: str | None = None
    buffer: list[str] = []
    open_entry = False

    def flush() -> None:
        nonlocal buffer, open_entry
        while buffer and not buffer[-1].strip():
            buffer.pop()
        if buffer:
            if section is None:
                raise ChangelogError("an entry appears before any `### ` section")
            sections[section].append("\n".join(buffer))
        buffer = []
        open_entry = False

    for line in body:
        if line.startswith("### "):
            flush()
            section = line[4:].strip()
            if section not in SECTIONS:
                raise ChangelogError(
                    f"{UNRELEASED} holds section `### {section}`; "
                    f"known sections are {', '.join(SECTIONS)}"
                )
            sections.setdefault(section, [])
            continue
        if line.startswith("- "):
            flush()
            buffer = [line]
            open_entry = True
            continue
        if not line.strip() and not open_entry:
            continue
        if not open_entry:
            raise ChangelogError(
                f"section `### {section}` holds text that is not an entry: {line.strip()[:60]!r}"
            )
        buffer.append(line)
    flush()
    return sections


def parse_changelog(text: str) -> tuple[str, dict[str, list[str]], str]:
    lines = text.split("\n")
    if UNRELEASED not in lines:
        raise ChangelogError(f"{CHANGELOG} has no `{UNRELEASED}` line")
    start = lines.index(UNRELEASED)
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    return (
        "\n".join(lines[: start + 1]),
        _parse_sections(lines[start + 1 : end]),
        "\n".join(lines[end:]),
    )


# meiva:note
# A section's first entry sits directly under its heading, and later entries are
# one blank line apart; a hand-merged section that broke either rule is rewritten
# to this shape.
def render_changelog(preamble: str, sections: dict[str, list[str]], trailer: str) -> str:
    out = [preamble]
    for name in SECTIONS:
        entries = sections.get(name) or []
        if not entries:
            continue
        out.append("")
        out.append(f"### {name}")
        out.append(entries[0])
        for entry in entries[1:]:
            out.append("")
            out.append(entry)
    rendered = "\n".join(out) + "\n"
    if trailer:
        rendered += "\n" + trailer
    return rendered


# meiva:note
# A fragment's age is the commit that added it, not its mtime: a clone, a rebase
# or a copy resets mtime, so ordering by it would reshuffle a finished entry. The
# Git query runs fixed argv against a path inside the repository.
def _merge_time(path: Path, root: Path) -> float:
    result = subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "log", "--diff-filter=A", "--format=%ct", "--", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    stamps = [int(line) for line in result.stdout.split() if line.isdigit()]
    return float(min(stamps)) if stamps else path.stat().st_mtime


def read_fragment(path: Path) -> Fragment:
    name = path.name
    if not _FRAGMENT_NAME.fullmatch(name):
        raise ChangelogError(
            f"{name}: name it `<pr>-<slug>.md`, e.g. `6566-timeline-card-chrome.md`"
        )
    text = path.read_text(encoding="utf-8")
    if not text.endswith("\n"):
        raise ChangelogError(f"{name}: the file must end with a newline")
    if text.endswith("\n\n"):
        raise ChangelogError(f"{name}: drop the blank line at the end of the file")
    lines = text[:-1].split("\n")
    match = _TYPE_LINE.fullmatch(lines[0]) if lines else None
    if match is None:
        raise ChangelogError(f"{name}: line 1 must be `type: <section>`, not {lines[0]!r}")
    if match.group(1) not in SECTIONS:
        raise ChangelogError(
            f"{name}: unknown section {match.group(1)!r}; use one of {', '.join(SECTIONS)}"
        )
    if len(lines) < 3 or lines[1] != "":
        raise ChangelogError(f"{name}: line 2 must be blank and line 3 must start the entry")
    entry = "\n".join(lines[2:])
    if not entry.startswith("- "):
        raise ChangelogError(f"{name}: the entry must start with `- `")
    if not entry[2:].strip():
        raise ChangelogError(f"{name}: the entry is empty")
    if "\n\n" in entry:
        raise ChangelogError(f"{name}: one entry per fragment; this one holds a blank line")
    return Fragment(path=path, section=match.group(1), entry=entry)


def read_fragments(root: Path) -> list[Fragment]:
    directory = root / FRAGMENT_DIR
    if not directory.is_dir():
        return []
    paths = sorted(p for p in directory.iterdir() if p.is_file())
    fragments = [read_fragment(path) for path in paths]
    return sorted(fragments, key=lambda f: (-_merge_time(f.path, root), f.path.name))


def fold_fragments(changelog: str, fragments: list[Fragment]) -> str:
    preamble, sections, trailer = parse_changelog(changelog)
    for fragment in fragments:
        sections.setdefault(fragment.section, []).insert(0, fragment.entry)
    return render_changelog(preamble, sections, trailer)


def _check(root: Path) -> int:
    directory = root / FRAGMENT_DIR
    if not directory.is_dir():
        return 0
    failures = 0
    for path in sorted(p for p in directory.iterdir() if p.is_file()):
        try:
            read_fragment(path)
        except ChangelogError as error:
            failures += 1
            print(f"{FRAGMENT_DIR}/{path.name}: {error}")
    return 1 if failures else 0


def main(argv: list[str] | None = None, *, root: Path = REPO_ROOT) -> int:
    parser = argparse.ArgumentParser(description="fold .changes/ fragments into CHANGELOG.md")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="validate the fragment files only")
    mode.add_argument(
        "--write", action="store_true", help="rewrite CHANGELOG.md and delete the fragments"
    )
    parser.add_argument("--root", type=Path, default=root, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root = args.root

    if args.check:
        return _check(root)

    changelog_path = root / CHANGELOG
    current = changelog_path.read_text(encoding="utf-8")
    fragments = read_fragments(root)
    try:
        updated = fold_fragments(current, fragments)
    except ChangelogError as error:
        print(f"{CHANGELOG}: {error}", file=sys.stderr)
        return 1

    if updated == current:
        print(f"{CHANGELOG}: unchanged ({len(fragments)} fragment(s))")
        return 0
    if not args.write:
        sys.stdout.writelines(
            difflib.unified_diff(
                current.splitlines(keepends=True),
                updated.splitlines(keepends=True),
                fromfile=f"a/{CHANGELOG}",
                tofile=f"b/{CHANGELOG}",
            )
        )
        return 0

    changelog_path.write_text(updated, encoding="utf-8")
    for fragment in fragments:
        fragment.path.unlink()
    print(f"{CHANGELOG}: folded {len(fragments)} fragment(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
