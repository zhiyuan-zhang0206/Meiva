# meiva:note
# What a change that pays recorded meizel debt down leaves behind when it removes a refusal
# instead of moving it: the hunks that drop a refusal message, the ones that soften it into a
# default, and which of them block the merge.
# meiva:link scripts/audit/docs/refusal-loss.meiva.okf.md
from __future__ import annotations

import re
from dataclasses import dataclass

DIFF_PATH = re.compile(r"^diff --git a/(.*) b/(.*)$")
_RAISE = re.compile(r"^\s*raise\b")
_FALLBACK = re.compile(
    r"^\s*(return\s+(None|False|0|\[\]|\{\}|\x22\x22|\x27\x27)\s*(#.*)?$)"
    r"|^\s*(pass|continue|break)\s*$"
    r"|^\s*except\b"
    r"|(\bor\s+(None|\[\]|\{\}|False)\b)"
    r"|(\.get\([^)]*,\s*(None|\[\]|\{\}|False|\x22\x22|\x27\x27)\s*\))"
    r"|(getattr\([^)]*,\s*(None|\[\]|\{\}|False)\s*\))"
)
_LITERAL = re.compile(r"""["']([^"']{6,})["']""")
_TEST_PATH = re.compile(r"(^|/)tests?/|(^|/)test_[^/]*\.py$")

DROPPED = "dropped-refusal"
DEFAULT = "default-allow"

# meiva:note
# Which hunks block: `unreplaced` is a hunk that deleted a refusal and added nothing that raises
# in its place; `deletion` narrows that to a hunk that adds nothing at all. `deletion` is the
# default because a merge gate can assert it without reading the change - the guard removed while
# the code it guarded stays - while the wider shape also flags hunks that only reworded a refusal.
UNREPLACED = "unreplaced"
DELETION = "deletion"


# meiva:note
# One `@@` hunk of a Python diff: what it removed and what it added.
@dataclass(frozen=True, slots=True)
class Hunk:
    path: str
    removed: tuple[str, ...]
    added: tuple[str, ...]


# meiva:note
# One hunk worth a reader's eyes, and whether it blocks the merge.
@dataclass(frozen=True, slots=True)
class Finding:
    commit: str
    subject: str
    path: str
    lens: str
    blocking: bool
    pure_deletion: bool
    detail: str


def _is_test_path(path: str) -> bool:
    return bool(_TEST_PATH.search(path))


# meiva:note
# Split `git show --unified=0 -- '*.py'` output into its hunks, each carrying the path of the file
# the diff header names.
def parse_diff(text: str) -> tuple[Hunk, ...]:
    hunks: list[Hunk] = []
    path: str = ""
    removed: list[str] = []
    added: list[str] = []
    open_hunk = False
    for line in text.split("\n"):
        match = DIFF_PATH.match(line)
        if match:
            path = match.group(2)
            continue
        if line.startswith("@@"):
            removed, added = [], []
            open_hunk = True
            hunks.append(Hunk(path, (), ()))
            continue
        if not open_hunk or line.startswith(("---", "+++")):
            continue
        if line.startswith("-"):
            removed.append(line[1:])
        elif line.startswith("+"):
            added.append(line[1:])
        else:
            continue
        hunks[-1] = Hunk(path, tuple(removed), tuple(added))
    return tuple(hunks)


# meiva:note
# Lines deleted minus lines added across the manifests a `--numstat` output covers: positive means
# the commit pays recorded debt down.
def manifest_delta(numstat: str) -> int:
    added = deleted = 0
    for line in numstat.split("\n"):
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        added += int(fields[0]) if fields[0].isdigit() else 0
        deleted += int(fields[1]) if fields[1].isdigit() else 0
    return deleted - added


def _raises(hunk: Hunk) -> tuple[str, ...]:
    return tuple(line for line in hunk.removed if _RAISE.match(line))


def _fallbacks(hunk: Hunk) -> tuple[str, ...]:
    return tuple(line for line in hunk.added if _FALLBACK.search(line))


# meiva:note
# Each removed statement, continued across the lines its parentheses still open, so a `raise`
# whose message sits on the next line stays one statement.
def _removed_statements(hunk: Hunk) -> tuple[tuple[str, ...], ...]:
    statements: list[tuple[str, ...]] = []
    index = 0
    lines = hunk.removed
    while index < len(lines):
        statement = [lines[index]]
        depth = lines[index].count("(") - lines[index].count(")")
        index += 1
        while depth > 0 and index < len(lines):
            statement.append(lines[index])
            depth += lines[index].count("(") - lines[index].count(")")
            index += 1
        statements.append(tuple(statement))
    return tuple(statements)


# meiva:note
# The literal texts the removed `raise` statements carry, continuation lines included.
def refusal_messages(hunk: Hunk) -> tuple[str, ...]:
    messages: list[str] = []
    for statement in _removed_statements(hunk):
        if not _RAISE.match(statement[0]):
            continue
        for line in statement:
            messages.extend(_LITERAL.findall(line))
    return tuple(dict.fromkeys(messages))


# meiva:note
# The refusal messages this hunk removed that the commit's tree no longer carries anywhere.
def _dropped(hunk: Hunk, tree_text: str) -> tuple[str, ...]:
    return tuple(message for message in refusal_messages(hunk) if message not in tree_text)


# meiva:note
# Whether the hunk adds a refusal of its own, which is what a moved check looks like.
def _replaces_refusal(hunk: Hunk) -> bool:
    return any(_RAISE.match(line) for line in hunk.added)


def _blocks(hunk: Hunk, rule: str) -> bool:
    if rule == DELETION:
        return not hunk.added
    return not _replaces_refusal(hunk)


# meiva:note
# Every hunk of one commit the two lenses flag, in diff order. A hunk whose file the commit
# deletes carries no refusal someone removed - the refusals left with the module - so a module
# deletion does not report its own body.
def findings_for(
    *,
    commit: str,
    subject: str,
    hunks: tuple[Hunk, ...],
    tree_text: str,
    existing: frozenset[str],
    rule: str = UNREPLACED,
) -> tuple[Finding, ...]:
    flagged: list[Finding] = []
    for hunk in hunks:
        if hunk.path not in existing or _is_test_path(hunk.path) or not _raises(hunk):
            continue
        first = _raises(hunk)[0].strip()
        gone = _dropped(hunk, tree_text)
        if gone:
            flagged.append(
                Finding(
                    commit=commit,
                    subject=subject,
                    path=hunk.path,
                    lens=DROPPED,
                    blocking=_blocks(hunk, rule),
                    pure_deletion=not hunk.added,
                    detail=f"{first[:96]} -> gone {list(gone[:2])}",
                )
            )
        for fallback in _fallbacks(hunk)[:1]:
            flagged.append(
                Finding(
                    commit=commit,
                    subject=subject,
                    path=hunk.path,
                    lens=DEFAULT,
                    blocking=False,
                    pure_deletion=not hunk.added,
                    detail=f"{first[:96]} / {fallback.strip()[:96]}",
                )
            )
    return tuple(flagged)


def blocking(findings: tuple[Finding, ...]) -> tuple[Finding, ...]:
    return tuple(finding for finding in findings if finding.blocking)


# meiva:note
# The line a pull request carries so a reader sees the check ran and what it found.
def summary_line(findings: tuple[Finding, ...]) -> str:
    return f"refusal-loss: {len(findings)} hits, {len(blocking(findings))} blocking"
