from __future__ import annotations

from collections import defaultdict

from meizel.contracts.analysis import Analysis
from meizel.contracts.manifest import first
from meizel.graph.model import Fact, MeizelError
from meizel.graph.universe import MANIFEST, parent_of
from meizel.language.fixtures import FixtureFunction, FixtureIndex, FixtureRequest
from meizel.language.scan import FileScan

__all__ = ["explain"]


def _inside(path: str, node: str) -> bool:
    return not node or path == node or path.startswith(f"{node}/")


def _group(facts: list[Fact], *, outgoing: bool) -> list[str]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for fact in facts:
        if outgoing:
            grouped[fact.dst].append(f"{fact.src}:{fact.line} {fact.kind}")
        else:
            grouped[fact.src].append(f"{fact.src}:{fact.line} {fact.kind} -> {fact.dst}")
    return [
        f"  {key}  <- {'; '.join(sorted(set(items))[:3])}" for key, items in sorted(grouped.items())
    ]


def _local_graph(analysis: Analysis, node: str) -> list[str]:
    parent = parent_of(node)
    name = node.rsplit("/", 1)[-1]
    manifest = analysis.manifests.get(parent)
    where = f"{parent or '.'}/{MANIFEST}"
    if manifest is None:
        return [f"parent {parent or '.'} has no {MANIFEST}: no local edges, exports only its door"]
    users = sorted(
        src for src, targets in manifest.uses.items() if any(first(t) == name for t in targets)
    )
    uses = sorted(
        t for src, targets in manifest.uses.items() if first(src) == name for t in targets
    )
    exported = "yes" if name in manifest.exports else "no"
    return [
        f"parent {parent or '.'} ({where})",
        f"  declared to use: {', '.join(uses) or '-'}",
        f"  declared to be used by: {', '.join(users) or '-'}",
        f"  exported by parent: {exported}",
    ]


def _directory_lines(analysis: Analysis, node: str) -> list[str]:
    manifest = analysis.manifests.get(node)
    exports = ", ".join(manifest.exports) if manifest and manifest.exports else "-"
    lines = [f"exports: {exports} (plus its __init__.py door)"]
    if manifest:
        lines.append("local graph:")
        lines.extend(f"  {src} -> {', '.join(t)}" for src, t in sorted(manifest.uses.items()))
    return lines


def explain(analysis: Analysis, node: str) -> str:
    universe = analysis.universe
    if node not in universe.files and node not in universe.dirs:
        raise MeizelError(f"{node or '.'}: not a tracked file or directory")
    is_dir = node in universe.dirs
    crossing = [f for f in analysis.facts if _inside(f.src, node) != _inside(f.dst, node)]
    uses = [f for f in crossing if _inside(f.src, node)]
    used_by = [f for f in crossing if _inside(f.dst, node)]
    lines = [f"{node or '.'} ({'directory' if is_dir else 'file'})"]
    if node:
        lines.extend(_local_graph(analysis, node))
    if is_dir:
        lines.extend(_directory_lines(analysis, node))
    lines.append(f"uses ({len(uses)} fact edges):")
    lines.extend(_group(uses, outgoing=True))
    lines.append(f"used by ({len(used_by)} fact edges):")
    lines.extend(_group(used_by, outgoing=False))
    lines.extend(_python_evidence(analysis, node))
    return "\n".join(lines)


def _python_evidence(analysis: Analysis, node: str) -> list[str]:
    scans = analysis.scans
    selected = {
        path: scan
        for path, scan in scans.items()
        if (path == node or not node or path.startswith(f"{node}/")) and path.endswith(".py")
    }
    if not selected:
        return []
    lines = [
        "Python source evidence (non-gating; no runtime execution or semantic purity proof):",
        "  input list observations cover direct append on un-rebound builtin-list annotations;",
        "  aliases, fields, nested scopes and other mutations are not resolved; absence is not a no-mutation proof.",
    ]
    for path, scan in sorted(selected.items()):
        lines.extend(
            f"  {path}:{item.line} {item.function}: {item.parameter}.append(...) "
            "on a list-annotated parameter (syntax; runtime type/execution unknown)"
            for item in scan.input_appends
        )
    lines.extend(_fixture_lines(analysis, selected))
    return lines


def _fixture_lines(analysis: Analysis, selected: dict[str, FileScan]) -> list[str]:
    scans = analysis.scans
    index = FixtureIndex(
        analysis.universe,
        {path: scan.fixtures.defs | scan.imported for path, scan in scans.items()},
        {path: [name for name, _ in scan.fixtures.plugins] for path, scan in scans.items()},
    )
    lines = [
        "  fixture request chains (declared syntax; resolved in the consuming test context):",
        "  class/nested requesters, autouse, module marks, imported fixture aliases and dynamic names",
        "  are not fully resolved; these chains are not a complete resource or static-admission assessment.",
    ]
    for path, scan in sorted(selected.items()):
        for function in scan.fixtures.functions:
            if function.registered is not None:
                continue
            label = f"{path}:{function.line} {function.name}"
            if not function.module_owned:
                lines.append(f"    {label}: unknown class/nested requester scope")
                continue
            roots = _owned_requests(scan, function)
            if not roots:
                lines.append(
                    f"    {label}: no explicit requests observed (implicit requests unknown)"
                )
            for request in roots:
                lines.extend(_request_lines(index, scans, path, request, (), [label]))
    return lines


def _owned_requests(scan: FileScan, function: FixtureFunction) -> list[FixtureRequest]:
    return [
        request for request in scan.fixtures.requests if request.requester_line == function.line
    ]


def _request_lines(
    index: FixtureIndex,
    scans: dict[str, FileScan],
    context: str,
    request: FixtureRequest,
    active: tuple[tuple[str, FixtureFunction], ...],
    trail: list[str],
) -> list[str]:
    trail = [*trail, f"{request.name} [{request.form}]"]
    function = _requester(scans, context, request, active)
    problem = _request_problem(request, function, active)
    if problem is not None:
        return [_render(trail, problem)]
    selected = _definition(index, scans, context, request, active)
    if isinstance(selected, str):
        return [_render(trail, selected)]
    path, definition = selected
    trail.append(f"{path}:{definition.line} {definition.name}")
    if any(path == p and definition.line == f.line for p, f in active):
        return [_render(trail, "recursive definition; execution unknown")]
    if len(active) >= 32:
        return [_render(trail, "display depth limit reached; remaining requests unknown")]
    requests = _owned_requests(scans[path], definition)
    if not requests:
        return [_render(trail, "resolved explicit chain; implicit requests unknown")]
    return [
        line
        for child in requests
        for line in _request_lines(
            index, scans, context, child, (*active, (path, definition)), trail
        )
    ]


def _requester(
    scans: dict[str, FileScan],
    context: str,
    request: FixtureRequest,
    active: tuple[tuple[str, FixtureFunction], ...],
) -> FixtureFunction | None:
    if active:
        return active[-1][1]
    return next(
        (f for f in scans[context].fixtures.functions if f.line == request.requester_line), None
    )


def _request_problem(
    request: FixtureRequest,
    function: FixtureFunction | None,
    active: tuple[tuple[str, FixtureFunction], ...],
) -> str | None:
    if request.form in {"lookup", "call"}:
        return "unknown receiver/execution; not a required fixture request"
    if request.form == "usefixtures" and active:
        return "marker on fixture definition; not a required request"
    if request.form == "parameter" and function is not None and not function.parameters_known:
        return "unknown parametrization: names or indirect mode not resolved"
    return None


def _definition(
    index: FixtureIndex,
    scans: dict[str, FileScan],
    context: str,
    request: FixtureRequest,
    active: tuple[tuple[str, FixtureFunction], ...],
) -> tuple[str, FixtureFunction] | str:
    candidates, ambiguous = index.candidates(request.name, context)
    depth = sum(definition.registered == request.name for _, definition in active)
    if depth >= len(candidates):
        return "unresolved fixture or exhausted same-name override chain"
    path = candidates[depth]
    if path in ambiguous:
        return "unknown plugin registration order"
    definitions = [f for f in scans[path].fixtures.functions if f.registered == request.name]
    if len(definitions) != 1 or not definitions[0].module_owned:
        return f"unknown definition ownership in {path}"
    return path, definitions[0]


def _render(trail: list[str], result: str) -> str:
    return f"    {' -> '.join(trail)}: {result}"
