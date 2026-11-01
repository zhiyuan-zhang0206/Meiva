# meiva:note
# Backstop: nothing outside `meizel/` re-lists the packages of a role.
#
# A Python tuple, list or set, or a TOML array, whose strings name every top-level directory
# of one role (of three or more directories) is a copy of the declaration in the root
# manifest: a package added to the role would silently miss it. Use
# `meizel.roles.select_packages(meizel.checkout.read_roles(repo), role)` or
# `python -m meizel roles list`, or a generated region, the only exception.
# Partial lists (a deliberate subset of a role) are not copies and are not reported.
#
# Tests, documentation and `meizel/` itself are outside the rule: a fixture lists packages on
# purpose, and meizel is where the declaration is read.
#
# A file that needs the list in a form only text can hold (a TOML array, a regular expression in
# YAML, a Python literal the application cannot import `meizel` for) carries a marked region:
#
#     # meizel:roles BEGIN names application tooling
#     "agent",
#     ...
#     # meizel:roles END
#
# `write` regenerates every region from the root manifest and `check` reports a region that is
# out of date. A file with a region is a generated output, and the only file outside `meizel/`
# that may list the packages of a role.

from __future__ import annotations

import ast
import hashlib
import json
import re
import sys
import tomllib
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard

from meizel import roles as role_api
from meizel.graph.model import Code, Diagnostic, MeizelError
from meizel.graph.universe import Checkout, EditPlan, Universe
from meizel.roles import Roles

__all__ = ["check", "parse_regions", "plan_writes", "regions", "run_roles", "write"]

MARKER = "meizel:roles"
_BEGIN = re.compile(rf"^(?P<indent>\s*)#\s*{MARKER}\s+BEGIN\s+(?P<format>\S+)\s+(?P<roles>.+?)\s*$")
_END = re.compile(rf"^\s*#\s*{MARKER}\s+END\s*$")
_TEXT_SUFFIXES = (".py", ".toml", ".yaml", ".yml", ".sh")


# meiva:note
# `begin` and `end` are the 1-based lines of the BEGIN and END markers.
@dataclass(frozen=True, slots=True)
class Region:
    path: str
    begin: int
    end: int
    indent: str
    format: str
    roles: tuple[str, ...]

    def render(self, declared: Mapping[str, tuple[str, ...]]) -> list[str]:
        names = role_api.select_packages(declared, *self.roles)
        if self.format == "names":
            return [f'{self.indent}"{name}",' for name in names]
        if self.format == "testpaths":
            hosts = [
                n if n in role_api.select_packages(declared, "tests") else f"{n}/**/tests"
                for n in names
            ]
            return [f'{self.indent}"{host}",' for host in hosts]
        if self.format == "dirs":
            return [f"{self.indent}{name}/" for name in names]
        if self.format == "regex-names":
            return [f"{self.indent}{'|'.join(names) if names else '(?!)'}"]
        raise MeizelError(f"{self.path}:{self.begin}: unknown generated format {self.format!r}")


# meiva:note
# The tracked files that mention the marker. Git is the source of tracked files, and the
# argument list is fixed.
def _candidates(universe: Universe) -> list[str]:
    return universe.repo.grep_files(MARKER, _TEXT_SUFFIXES)


def parse_regions(texts: Mapping[str, str]) -> list[Region]:
    found: list[Region] = []
    for path, content in texts.items():
        lines = content.splitlines()
        open_region: re.Match[str] | None = None
        start = 0
        for number, text in enumerate(lines, 1):
            begin = _BEGIN.match(text)
            if begin:
                if open_region is not None:
                    raise MeizelError(f"{path}:{number}: a region begins inside the one at {start}")
                open_region, start = begin, number
            elif _END.match(text):
                if open_region is None:
                    raise MeizelError(f"{path}:{number}: END without BEGIN")
                found.append(
                    Region(
                        path,
                        start,
                        number,
                        open_region["indent"],
                        open_region["format"],
                        tuple(open_region["roles"].split()),
                    )
                )
                open_region = None
        if open_region is not None:
            raise MeizelError(f"{path}:{start}: BEGIN without END")
    return found


def _candidate_texts(universe: Universe) -> dict[str, str]:
    return {path: universe.repo.load_text(path) for path in _candidates(universe)}


# meiva:note
# Every marked region of every tracked file outside `meizel/`.
def regions(universe: Universe, texts: Mapping[str, str] | None = None) -> list[Region]:
    source = _candidate_texts(universe) if texts is None else texts
    return parse_regions(source)


# meiva:note
# One message per region whose content differs from what the roles generate.
def _stale(
    texts: Mapping[str, str], items: list[Region], declared: Mapping[str, tuple[str, ...]]
) -> list[Diagnostic]:
    messages: list[Diagnostic] = []
    for region in items:
        lines = texts[region.path].splitlines()
        if lines[region.begin : region.end - 1] != region.render(declared):
            messages.append(
                Diagnostic(
                    region.path,
                    region.begin,
                    Code.MANIFEST_ROLES,
                    f"generated {region.format} region is stale; "
                    "run `python -m meizel roles write`",
                )
            )
    return messages


def plan_writes(
    root: Path,
    texts: Mapping[str, str],
    by_path: Mapping[str, list[Region]],
    declared: Mapping[str, tuple[str, ...]],
) -> tuple[EditPlan, list[str]]:
    writes: dict[Path, str] = {}
    changed: list[str] = []
    for path, items in by_path.items():
        text = texts[path]
        lines = text.splitlines()
        for region in sorted(items, key=lambda r: r.begin, reverse=True):
            lines[region.begin : region.end - 1] = region.render(declared)
        updated = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
        if updated != text:
            writes[root / path] = updated
            changed.append(path)
    return EditPlan(writes=writes), changed


# meiva:note
# Regenerate every region; returns one line per file rewritten.
def write(repo: Checkout, declared: Roles) -> list[str]:
    universe = repo.universe()
    texts = _candidate_texts(universe)
    by_path: dict[str, list[Region]] = {}
    for region in parse_regions(texts):
        by_path.setdefault(region.path, []).append(region)
    plan, changed = plan_writes(repo.root, texts, by_path, declared)
    repo.apply_plan(plan)
    return changed


_MIN_ROLE_SIZE = 3
_FIRST = re.compile(r"[/.*\\]")
_TESTS_DIR = re.compile(r"(^|/)tests?/")


def _outside_rule(path: str) -> bool:
    return (
        path.startswith(("meizel/", "docs/"))
        or _TESTS_DIR.search(path) is not None
        or path.rsplit("/", 1)[-1] == "conftest.py"
    )


def _listed(strings: Iterable[str]) -> set[str]:
    return {_FIRST.split(text, 1)[0] for text in strings if text}


def _covered(strings: Iterable[str], declared: Mapping[str, tuple[str, ...]]) -> list[str]:
    names = _listed(strings)
    return [
        role
        for role, members in declared.items()
        if len(members) >= _MIN_ROLE_SIZE and set(members) <= names
    ]


def _message(role: str) -> str:
    return (
        f"lists every top-level package of role {role!r}; read it with "
        f"`python -m meizel roles list {role}` or generate it (see meizel/contracts/enumerations.py)"
    )


def _python(
    path: str, text: str, declared: Mapping[str, tuple[str, ...]], spans: list[tuple[int, int]]
) -> list[Diagnostic]:
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError:
        return []
    found: list[Diagnostic] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Tuple | ast.List | ast.Set):
            continue
        strings = [
            e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)
        ]
        if len(strings) < _MIN_ROLE_SIZE:
            continue
        end = node.end_lineno or node.lineno
        if any(node.lineno <= begin and finish <= end for begin, finish in spans):
            continue
        found.extend(
            Diagnostic(path, node.lineno, Code.MANIFEST_ROLES, _message(role))
            for role in _covered(strings, declared)
        )
    return found


def _is_raw_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _is_table(value: object) -> TypeGuard[dict[str, object]]:
    return _is_raw_dict(value) and all(isinstance(k, str) for k in value)


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _arrays(value: object, trail: str) -> Iterable[tuple[str, list[str]]]:
    if _is_table(value):
        for key, child in value.items():
            yield from _arrays(child, f"{trail}.{key}" if trail else key)
    elif _is_object_list(value):
        strings = [x for x in value if isinstance(x, str)]
        if strings:
            yield trail, strings
        for child in value:
            yield from _arrays(child, trail)


def _toml(
    path: str, text: str, declared: Mapping[str, tuple[str, ...]], spans: list[tuple[int, int]]
) -> list[Diagnostic]:
    lines = text.splitlines()
    for begin, end in spans:
        for index in range(begin, end - 1):
            lines[index] = ""
    try:
        document = tomllib.loads("\n".join(lines))
    except tomllib.TOMLDecodeError:
        return []
    found: list[Diagnostic] = []
    for trail, strings in _arrays(document, ""):
        key = trail.rsplit(".", 1)[-1]
        pattern = re.compile(rf"""^\s*["']?{re.escape(key)}["']?\s*=""")
        line = next((n for n, x in enumerate(lines, 1) if pattern.match(x)), 1)
        found.extend(
            Diagnostic(path, line, Code.MANIFEST_ROLES, _message(role))
            for role in _covered(strings, declared)
        )
    return found


def _copy_candidates(
    universe: Universe,
    eligible: list[tuple[str, ...]],
    changed: Collection[str] | None,
) -> list[str]:
    if not eligible:
        return []
    anchors = sorted({max(members, key=len) for members in eligible})
    hits: set[str] = set()
    for anchor in anchors:
        hits.update(universe.repo.grep_files(anchor, (".py", ".toml")))
    if changed is not None:
        hits.update(p for p in changed if p.endswith((".py", ".toml")))
    return sorted(
        p for p in hits if p in universe.files and not _outside_rule(p) and p != "meizel.toml"
    )


def _roles_cache_key(universe: Universe, declared: Mapping[str, tuple[str, ...]]) -> str:
    digests = universe.file_digests()
    if not digests:
        return ""
    text_files = [p for p in sorted(universe.files) if p.endswith(_TEXT_SUFFIXES)]
    if any(p not in digests for p in text_files):
        return ""
    hasher = hashlib.sha256()
    roles_json = json.dumps(
        [[k, list(v)] for k, v in sorted(declared.items())], separators=(",", ":")
    )
    hasher.update(f"{universe.repo.scanner_digest()}\0{roles_json}\0".encode())
    for path in text_files:
        hasher.update(f"{path}:{digests[path]}\0".encode())
    return f"roles:v1:{hasher.hexdigest()}"


def _decode_cached_roles(text: str) -> list[Diagnostic] | None:
    try:
        raw: object = json.loads(text)
    except ValueError:
        return None
    if not _is_object_list(raw):
        return None
    found: list[Diagnostic] = []
    for item in raw:
        if not _is_object_list(item) or len(item) != 4:
            return None
        path, line, code, msg = item
        if (
            not isinstance(path, str)
            or type(line) is not int
            or not isinstance(code, str)
            or not isinstance(msg, str)
        ):
            return None
        try:
            found.append(Diagnostic(path, line, Code(code), msg))
        except ValueError:
            return None
    return found


def _load_cached_roles(universe: Universe, cache_key: str) -> list[Diagnostic] | None:
    if not cache_key:
        return None
    hit = universe.repo.cache_get_many((cache_key,)).get(cache_key)
    return None if hit is None else _decode_cached_roles(hit)


def _store_cached_roles(universe: Universe, cache_key: str, result: list[Diagnostic]) -> None:
    if not cache_key:
        return
    encoded = json.dumps(
        [[d.path, d.line, d.code.value, d.message] for d in result], separators=(",", ":")
    )
    universe.repo.cache_put_many({cache_key: encoded})


def _scan_copy_candidates(
    universe: Universe,
    declared: Mapping[str, tuple[str, ...]],
    spans: Mapping[str, list[tuple[int, int]]],
    changed: Collection[str] | None,
) -> list[Diagnostic]:
    eligible = [members for members in declared.values() if len(members) >= _MIN_ROLE_SIZE]
    found: list[Diagnostic] = []
    for path in _copy_candidates(universe, eligible, changed):
        try:
            text = universe.repo.load_text(path)
        except (OSError, UnicodeDecodeError):
            continue
        if not any(all(pkg in text for pkg in members) for members in eligible):
            continue
        scan = _python if path.endswith(".py") else _toml
        found.extend(scan(path, text, declared, spans.get(path, [])))
    return found


# meiva:note
# Global generated freshness and package-list copies, using validated declarations.
def check(
    universe: Universe,
    declared: Mapping[str, tuple[str, ...]],
    *,
    changed: Collection[str] | None = None,
) -> list[Diagnostic]:
    cache_key = _roles_cache_key(universe, declared)
    cached = _load_cached_roles(universe, cache_key)
    if cached is not None:
        return cached
    texts = _candidate_texts(universe)
    items = parse_regions(texts)
    if not declared and not items:
        _store_cached_roles(universe, cache_key, [])
        return []
    spans: dict[str, list[tuple[int, int]]] = {}
    for region in items:
        spans.setdefault(region.path, []).append((region.begin, region.end))
    found = _stale(texts, items, declared)
    if items and not declared:
        found.append(
            Diagnostic(
                "meizel.toml",
                1,
                Code.MANIFEST_ROLES,
                "generated role regions require a root [roles] declaration",
            )
        )
    found.extend(_scan_copy_candidates(universe, declared, spans, changed))
    result = sorted(found, key=lambda d: (d.path, d.line, d.message))
    _store_cached_roles(universe, cache_key, result)
    return result


def run_roles(repo: Checkout, declared: Roles, mode: str, role_names: tuple[str, ...] = ()) -> int:
    if mode == "list":
        for name in role_api.select_packages(declared, *role_names):
            print(name)
        return 0
    if mode == "projects":
        for project in repo.universe().standalone_projects:
            print(project.directory)
        return 0
    if mode == "write":
        for path in write(repo, declared):
            print(f"meizel: regenerated {path}")
        return 0
    problems = check(repo.universe(), declared)
    for problem in problems:
        print(problem.render())
    if problems:
        print(f"meizel: {len(problems)} role problems", file=sys.stderr)
        return 1
    print("meizel: roles ok")
    return 0
