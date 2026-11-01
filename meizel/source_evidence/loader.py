# meiva:note
# Explicit index admission reads the supplied artifact, selected source snapshot and tool inputs.
# The established UTF-8 reader owns disk access; this loader validates identity, not semantic purity.
# meiva:link meizel/docs/checker/typescript-evidence.meiva.okf.md

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TypeGuard

from meizel.graph.model import MeizelError
from meizel.lint_common import read_utf8_text, tracked_files
from meizel.source_evidence.decode import decode_index
from meizel.source_evidence.model import Index, TsReport

_ADAPTER = "meizel/source_evidence/typescript/"
_COMPILER = "ui/web/node_modules/typescript/lib/"
_LOCK = "ui/web/pnpm-lock.yaml"
_PACKAGE = "ui/web/node_modules/typescript/package.json"


# meiva:note
# Where the evidence is read from, as one value the entry resolves: `root` is the judged checkout
# (the index names it), and `tool` is the installation whose adapter and locked compiler the index
# is checked against. Neither is derived here.
@dataclass(frozen=True, slots=True)
class Evidence:
    root: Path
    tool: Path


def ts_paths(files: set[str], node: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            path
            for path in files
            if (path == node or not node or path.startswith(f"{node}/"))
            and path.endswith((".ts", ".tsx"))
            and not path.endswith(".d.ts")
        )
    )


def _text(root: Path, relative: str) -> str:
    path = PurePosixPath(relative)
    if path.is_absolute() or str(path) != relative or ".." in path.parts or "\\" in relative:
        raise MeizelError(f"invalid TS index path: {relative!r}")
    text = read_utf8_text(root / relative)
    if text is None:
        raise MeizelError(f"unreadable TS index input: {relative}")
    return text


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dependencies(index: Index, evidence: Evidence) -> dict[str, str]:
    expected = {
        _LOCK,
        _PACKAGE,
        "meizel/language/typescript_compiler/compiler.mjs",
        f"{_COMPILER}typescript.js",
        *(p for p in tracked_files(evidence.tool) if p.startswith(_ADAPTER) and p.endswith(".mjs")),
    }
    contents: dict[str, str] = {}
    for item in index.dependencies:
        library = item.path.startswith(_COMPILER) and item.path.endswith(".d.ts")
        if item.path in contents or (item.path not in expected and not library):
            raise MeizelError(f"unexpected or duplicate TS dependency: {item.path}")
        content = _text(evidence.tool, item.path)
        if _digest(content) != item.sha256:
            raise MeizelError(f"stale TS dependency: {item.path}")
        contents[item.path] = content
    if not expected <= contents.keys():
        raise MeizelError("TS index omitted required tool dependencies")
    locked = _locked_typescript_version(contents[_LOCK])
    if index.compiler_version != locked or index.compiler_version != _package_version(
        contents[_PACKAGE]
    ):
        raise MeizelError("TS compiler version does not match the locked installation")
    return contents


def _is_raw_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _is_table(value: object) -> TypeGuard[dict[str, object]]:
    return _is_raw_dict(value) and all(isinstance(k, str) for k in value)


def _package_version(text: str) -> str:
    try:
        value: object = json.loads(text)
    except ValueError as error:
        raise MeizelError("invalid TS compiler installation metadata") from error
    if not _is_table(value) or "version" not in value:
        raise MeizelError("invalid TS compiler installation metadata")
    version = value["version"]
    if not isinstance(version, str):
        raise MeizelError("invalid TS compiler version")
    return version


_TS_IMPORTER_SCOPES = frozenset(
    {
        ("importers", ".", "dependencies", "typescript"),
        ("importers", ".", "devDependencies", "typescript"),
    }
)


def _unquote(raw: str) -> str:
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def _lock_body_lines(text: str) -> list[str]:
    lines = [
        line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]
    if not lines or not lines[0].startswith("lockfileVersion:"):
        raise MeizelError("invalid TS compiler installation metadata")
    if _unquote(lines[0].partition(":")[2]) != "9.0":
        raise MeizelError("invalid TS compiler installation metadata")
    return lines[1:]


def _yaml_line_entry(line: str) -> tuple[int, str, str] | None:
    indent = len(line) - len(line.lstrip(" "))
    key, sep, rest = line.strip().partition(":")
    if not sep or indent % 2:
        return None
    return indent // 2, _unquote(key), rest


def _locked_typescript_version(text: str) -> str:
    scope: tuple[str, ...] = ("", "", "", "")
    for line in _lock_body_lines(text):
        parsed = _yaml_line_entry(line)
        if parsed is None:
            continue
        level, clean_key, rest = parsed
        if level == 0 and scope[0] == "importers":
            break
        if level < 4:
            scope = (*scope[:level], clean_key, *("",) * (3 - level))
        elif level == 4 and scope in _TS_IMPORTER_SCOPES and clean_key == "version":
            version = _unquote(rest).partition("(")[0].strip()
            if version:
                return version
            break
    raise MeizelError("invalid TS compiler installation metadata")


def _sources(
    index: Index, root: Path, selected: tuple[str, ...], dependencies: Mapping[str, str]
) -> None:
    seen: set[str] = set()
    for source in index.sources:
        if source.path not in selected or source.path in seen:
            raise MeizelError(f"unexpected or duplicate TS source: {source.path}")
        text = _text(root, source.path)
        if _digest(text) != source.sha256:
            raise MeizelError(f"stale TS source: {source.path}")
        lines = len(text.split("\n"))
        for item in source.observations:
            if item.end_line > lines:
                raise MeizelError(f"TS observation outside source: {source.path}:{item.line}")
            for declaration in (*item.declarations, *item.freeze_receiver, *item.freeze_callee):
                if (
                    declaration.dependency not in dependencies
                    or not declaration.dependency.startswith(_COMPILER)
                    or not declaration.dependency.endswith(".d.ts")
                ):
                    raise MeizelError("TS allocation lacks locked library provenance")
                if declaration.line > len(dependencies[declaration.dependency].split("\n")):
                    raise MeizelError("TS declaration outside library snapshot")
        seen.add(source.path)
    if seen != set(selected):
        raise MeizelError("TS index omitted selected sources")


def load_report(evidence: Evidence, files: set[str], node: str, supplied: Path | None) -> TsReport:
    selected = ts_paths(files, node)
    if supplied is None:
        status = "not-requested" if selected else "not-applicable"
        return TsReport(status=status, sources=())
    text = read_utf8_text(supplied)
    if text is None:
        raise MeizelError(f"unreadable TS source index: {supplied}")
    try:
        index = decode_index(text)
    except (TypeError, ValueError) as error:
        raise MeizelError(f"invalid TS source index: {error}") from error
    if index.root != str(evidence.root):
        raise MeizelError("TS index repository root does not match")
    dependencies = _dependencies(index, evidence)
    _sources(index, evidence.root, selected, dependencies)
    status = (
        "not-applicable"
        if not selected
        else "unavailable"
        if all(s.status == "unavailable" for s in index.sources)
        else "observed"
    )
    return TsReport(status=status, sources=index.sources)
