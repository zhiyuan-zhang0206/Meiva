# meiva:note
# Explicit index admission reads the supplied artifact, selected source snapshot and tool inputs.
# The established UTF-8 reader owns disk access; this loader validates identity, not semantic purity.
# meiva:link meizel/docs/checker/typescript-evidence.meiva.okf.md

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import cast

from meizel.graph.model import MeizelError
from meizel.lint_common import read_utf8_text, tracked_files
from meizel.source_evidence.decode import decode_index
from meizel.source_evidence.model import Index, TsReport

_ADAPTER = "meizel/source_evidence/typescript/"
_COMPILER = "ui/web/node_modules/typescript/lib/"
_LOCK = "ui/web/package-lock.json"
_PACKAGE = "ui/web/node_modules/typescript/package.json"


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
    if (
        path.is_absolute()
        or str(path) != relative
        or ".." in path.parts
        or "\\" in relative
        or not (root / relative).resolve().is_relative_to(root.resolve())
    ):
        raise MeizelError(f"invalid TS index path: {relative!r}")
    text = read_utf8_text(root / relative)
    if text is None:
        raise MeizelError(f"unreadable TS index input: {relative}")
    return text


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dependencies(index: Index, tool: Path) -> dict[str, str]:
    expected = {
        _LOCK,
        _PACKAGE,
        f"{_COMPILER}typescript.js",
        *(p for p in tracked_files(tool) if p.startswith(_ADAPTER) and p.endswith(".mjs")),
    }
    contents: dict[str, str] = {}
    for item in index.dependencies:
        library = item.path.startswith(_COMPILER) and item.path.endswith(".d.ts")
        if item.path in contents or (item.path not in expected and not library):
            raise MeizelError(f"unexpected or duplicate TS dependency: {item.path}")
        content = _text(tool, item.path)
        if _digest(content) != item.sha256:
            raise MeizelError(f"stale TS dependency: {item.path}")
        contents[item.path] = content
    if not expected <= contents.keys():
        raise MeizelError("TS index omitted required tool dependencies")
    locked = _version(contents[_LOCK], ("packages", "node_modules/typescript", "version"))
    if index.compiler_version != locked or index.compiler_version != _version(
        contents[_PACKAGE], ("version",)
    ):
        raise MeizelError("TS compiler version does not match the locked installation")
    return contents


def _version(text: str, keys: tuple[str, ...]) -> str:
    try:
        value: object = json.loads(text)
    except ValueError as error:
        raise MeizelError("invalid TS compiler installation metadata") from error
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            raise MeizelError("invalid TS compiler installation metadata")
        value = cast("dict[str, object]", value)[key]
    if not isinstance(value, str):
        raise MeizelError("invalid TS compiler version")
    return value


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


def load_report(root: Path, files: set[str], node: str, supplied: Path | None) -> TsReport:
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
    if index.root != str(root.resolve()):
        raise MeizelError("TS index repository root does not match")
    tool = Path(__file__).resolve().parents[2]
    dependencies = _dependencies(index, tool)
    _sources(index, root, selected, dependencies)
    status = (
        "not-applicable"
        if not selected
        else "unavailable"
        if all(s.status == "unavailable" for s in index.sources)
        else "observed"
    )
    return TsReport(status=status, sources=index.sources)
