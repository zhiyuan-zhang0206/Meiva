# meiva:note
# Answers to hints. A commit message answers a hint with a trailer line
#
#   Hint-Ack: <hint-id> <path>[:<line>] -- <reason>
#
# One trailer answers one site; a path without a line answers every site of that id in
# that file, and a directory (a path ending in `/`) every site of that id below it. A line that starts with `Hint-Ack:` and does not parse is an error rather
# than a silent non-answer, so a typo cannot pass for an answer.
from __future__ import annotations

import re
from collections.abc import Iterable

from scripts.lint.diagnostics.diff_hints.model import Ack, HintId, Site

_ACK_LINE = re.compile(
    r"^Hint-Ack:[ \t]+(?P<hint>\S+)[ \t]+(?P<path>\S+?)(?::(?P<line>\d+))?[ \t]+--[ \t]+"
    r"(?P<reason>\S.*?)[ \t]*$"
)
_PREFIX = "Hint-Ack:"
_MIN_REASON = 8
_PLACEHOLDER = "<reason>"
_TEMPLATES_PER_HINT = 6


def _parse_line(line: str) -> tuple[Ack | None, str | None]:
    found = _ACK_LINE.match(line)
    if found is None:
        return (
            None,
            f"malformed trailer, expected `Hint-Ack: <hint-id> <path>[:<line>] -- <reason>`: {line}",
        )
    try:
        hint = HintId(found.group("hint"))
    except ValueError:
        known = ", ".join(member.value for member in HintId)
        return None, f"unknown hint id `{found.group('hint')}` (known: {known}): {line}"
    reason = found.group("reason")
    if len(reason) < _MIN_REASON or _PLACEHOLDER in reason:
        return None, f"the reason must say why the site is fine, in a sentence: {line}"
    number = found.group("line")
    return Ack(hint, found.group("path"), int(number) if number else None, reason), None


# meiva:note
# The acks and the error lines of one commit message. Lines that start with `#` are
# git's own template comments and are skipped.
def parse_acks(message: str) -> tuple[tuple[Ack, ...], tuple[str, ...]]:
    acks: list[Ack] = []
    errors: list[str] = []
    for raw in message.splitlines():
        line = raw.strip()
        if not line.startswith(_PREFIX):
            continue
        ack, error = _parse_line(line)
        if ack is not None:
            acks.append(ack)
        if error is not None:
            errors.append(error)
    return tuple(acks), tuple(errors)


def _answers(ack: Ack, site: Site, *, ignore_line: bool) -> bool:
    if ack.hint is not site.hint:
        return False
    if ack.path.endswith("/"):
        return ack.line is None and site.path.startswith(ack.path)
    if ack.path != site.path:
        return False
    return ack.line is None or ignore_line or ack.line == site.line


# meiva:note
# The sites no ack answers. `ignore_line` is the branch-wide check: a line number in a
# trailer is only reliable in the commit that wrote it (a later commit moves lines), so
# there a trailer with a line answers by id and path.
def unanswered(sites: Iterable[Site], acks: Iterable[Ack], *, ignore_line: bool) -> list[Site]:
    given = tuple(acks)
    return [s for s in sites if not any(_answers(a, s, ignore_line=ignore_line) for a in given)]


def unused(acks: Iterable[Ack], sites: Iterable[Site]) -> list[Ack]:
    present = tuple(sites)
    return [a for a in acks if not any(_answers(a, s, ignore_line=False) for s in present)]


# meiva:note
# The finest directory grain that keeps the template list short: the files' own
# directories, then their first three, two and one directory segments.
def _directory_keys(paths: list[str]) -> list[str]:
    keys = sorted({path.rsplit("/", 1)[0] + "/" for path in paths})
    for depth in (3, 2, 1):
        if len(keys) <= _TEMPLATES_PER_HINT:
            break
        keys = sorted({"/".join(path.split("/")[:-1][:depth]) + "/" for path in paths})
    return keys


# meiva:note
# Trailer templates for the sites left open: a site alone is answered at its line, several
# sites of one id in one file by a path-level trailer, and an id open in many files by a
# few directory-level trailers.
def suggestions(sites: Iterable[Site]) -> list[str]:
    grouped: dict[HintId, dict[str, list[Site]]] = {}
    for site in sites:
        grouped.setdefault(site.hint, {}).setdefault(site.path, []).append(site)
    lines: list[str] = []
    for hint, by_path in grouped.items():
        if len(by_path) > _TEMPLATES_PER_HINT:
            targets = _directory_keys(list(by_path))
        else:
            targets = [
                path if len(members) > 1 else f"{path}:{members[0].line}"
                for path, members in by_path.items()
            ]
        lines.extend(f"Hint-Ack: {hint.value} {target} -- {_PLACEHOLDER}" for target in targets)
    return lines
