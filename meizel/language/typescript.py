from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass

from meizel import allowances
from meizel.allowances import Policy
from meizel.graph.model import Code, Fact, FactKind, Patch, Problem, PurityItem
from meizel.graph.universe import Universe, parent_of
from meizel.language import fixtures

__all__ = [
    "TsConfig",
    "TsScan",
    "load_ts_config",
    "scan_typescript",
    "typescript_files",
]

DEFAULT_TS_IMPURE_CALLS: frozenset[str] = frozenset(
    {
        "Date.now",
        "Math.random",
        "PerformanceObserver",
        "BroadcastChannel",
        "EventSource",
        "IntersectionObserver",
        "MutationObserver",
        "Notification",
        "ResizeObserver",
        "SharedWorker",
        "WebSocket",
        "Worker",
        "XMLHttpRequest",
        "alert",
        "cancelAnimationFrame",
        "clearInterval",
        "clearTimeout",
        "confirm",
        "console.debug",
        "console.error",
        "console.info",
        "console.log",
        "console.warn",
        "crypto.getRandomValues",
        "crypto.randomUUID",
        "document",
        "fetch",
        "history",
        "localStorage",
        "location",
        "navigator",
        "performance.now",
        "process.env",
        "prompt",
        "queueMicrotask",
        "requestAnimationFrame",
        "sessionStorage",
        "setInterval",
        "setTimeout",
        "window",
    }
)

DEFAULT_TS_IMPURE_HOOKS: frozenset[str] = frozenset(
    {
        "createContext",
        "useActionState",
        "useContext",
        "useDeferredValue",
        "useEffect",
        "useId",
        "useInfiniteQuery",
        "useInsertionEffect",
        "useLayoutEffect",
        "useMutation",
        "useOptimistic",
        "useQuery",
        "useQueryClient",
        "useReducer",
        "useRef",
        "useState",
        "useSuspenseQuery",
        "useSyncExternalStore",
        "useTransition",
    }
)

_TOKEN_RE = re.compile(
    r"""'(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*"|`(?:\\.|[^`\\])*`|//[^\n]*|/\*[\s\S]*?\*/"""
)
_IMPORT_FROM_RE = re.compile(r"""\b(import|export)\s+([^;]*?)\s+from\s*["']([^"'\n]+)["']""")
_SIDE_EFFECT_IMPORT_RE = re.compile(r"""^\s*import\s*["']([^"'\n]+)["']""", re.MULTILINE)
_DYNAMIC_IMPORT_RE = re.compile(r"""\bimport\s*\(\s*["']([^"'\n]+)["']\s*\)""")
_VI_MOCK_RE = re.compile(r"""\b(vi\.(?:mock|doMock))\s*\(\s*["']([^"'\n]+)["']""")
_VI_SPY_RE = re.compile(r"""\b(vi\.spyOn)\s*\(\s*([A-Za-z_$][\w$.]*)\s*,\s*["']([^"'\n]+)["']""")
_VI_STUB_ENV_RE = re.compile(r"""\b(vi\.stubEnv)\s*\(\s*["']([^"'\n]+)["']""")
_VI_STUB_GLOBAL_RE = re.compile(r"""\b(vi\.stubGlobal)\s*\(\s*["']([^"'\n]+)["']""")
_EXPORT_DECL_RE = re.compile(
    r"""^[ \t]*export\s+(?:async\s+)?(function|const|let|var|class|interface|type|enum)\s+([A-Za-z_$][\w$]*)""",
    re.MULTILINE,
)
_EXPORT_LIST_RE = re.compile(r"""^[ \t]*export\s*(?:type\s*)?\{([^}]+)\}""", re.MULTILINE)
_TOP_FUNC_RE = re.compile(
    r"""^[ \t]*(export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*(?:<[^(\n]*>)?\s*\(""",
    re.MULTILINE,
)
_TOP_MUTABLE_RE = re.compile(
    r"""^(?:export\s+)?(?:let|var)\s+([A-Za-z_$][\w$]*)\b""",
    re.MULTILINE,
)
_TOP_COLLECTION_RE = re.compile(
    r"""^(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*(?::[^=\n]+)?=\s*new\s+(?:Map|Set)\b""",
    re.MULTILINE,
)
_PASSTHROUGH_BODY_RE = re.compile(
    r"""^\{\s*return\s+(?:await\s+)?([A-Za-z_$][\w$.]*)\s*\(([^()]*)\)\s*;?\s*\}$"""
)
_MEMBER_CALL_RE = re.compile(r"""\b([A-Za-z_$][\w$]*)\.([A-Za-z_$][\w$]*)\b""")
_NEW_CALL_RE = re.compile(r"""\bnew\s+([A-Za-z_$][\w$]*)\s*\(""")
_DIRECT_CALL_RE = re.compile(r"""(?<!\.)\b([A-Za-z_$][\w$]*)\s*(?:<[^(\n]*>)?\s*\(""")


@dataclass(frozen=True, slots=True)
class TsConfig:
    scopes: tuple[str, ...]
    import_sources: tuple[str, ...]
    aliases: tuple[tuple[str, str], ...]
    extensions: tuple[str, ...]
    impure_calls: frozenset[str]
    impure_hooks: frozenset[str]
    max_public_symbols: int
    max_params: int


@dataclass(frozen=True, slots=True)
class TsScan:
    path: str
    facts: tuple[Fact, ...]
    external: frozenset[str]
    patches: tuple[Patch, ...]
    purity: tuple[PurityItem, ...]
    problems: tuple[Problem, ...]
    exports_all: frozenset[str] | None
    defined: frozenset[str]


def _policy_int(policy: Policy, section: str, default: int) -> int:
    raw = policy.names("purity", section)
    return int(raw[0]) if raw and raw[0].isdigit() else default


def _parse_aliases(entries: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    for item in entries:
        prefix, sep, target = item.partition("=")
        if sep and prefix and target:
            pairs.append((prefix.strip(), target.strip()))
    return tuple(pairs)


def load_ts_config(policy: Policy) -> TsConfig:
    calls = frozenset(policy.names("typescript", "impure-calls")) or DEFAULT_TS_IMPURE_CALLS
    hooks = frozenset(policy.names("typescript", "impure-hooks")) or DEFAULT_TS_IMPURE_HOOKS
    exts = policy.names("typescript", "extensions") or (".ts", ".tsx")
    return TsConfig(
        scopes=policy.names("typescript", "scopes"),
        import_sources=policy.names("typescript", "import-sources"),
        aliases=_parse_aliases(policy.names("typescript", "aliases")),
        extensions=exts,
        impure_calls=calls,
        impure_hooks=hooks,
        max_public_symbols=_policy_int(policy, "max-public-symbols", 7),
        max_params=_policy_int(policy, "max-params", 5),
    )


def _under_any(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == p or path.startswith(f"{p}/") for p in prefixes)


def typescript_files(universe: Universe) -> tuple[str, ...]:
    config = load_ts_config(allowances.load(universe.root))
    active = (*config.scopes, *config.import_sources)
    if not active:
        return ()
    return tuple(
        p
        for p in universe.typescript_files
        if p.endswith(config.extensions) and _under_any(p, active)
    )


def _blank_non_newlines(text: str) -> str:
    return re.sub(r"[^\n]", " ", text)


def _strip_comments_and_strings(source: str) -> tuple[str, str]:
    uncommented: list[str] = []
    code_only: list[str] = []
    last = 0
    for match in _TOKEN_RE.finditer(source):
        start, end = match.span()
        chunk = source[last:start]
        uncommented.append(chunk)
        code_only.append(chunk)
        token = match.group(0)
        if token.startswith(("//", "/*")):
            blank = _blank_non_newlines(token)
            uncommented.append(blank)
            code_only.append(blank)
        else:
            uncommented.append(token)
            quote = token[0]
            code_only.append(f"{quote}{_blank_non_newlines(token[1:-1])}{quote}")
        last = end
    tail = source[last:]
    uncommented.append(tail)
    code_only.append(tail)
    return "".join(uncommented), "".join(code_only)


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _resolve_candidate(base: str, universe: Universe, extensions: tuple[str, ...]) -> str | None:
    if base in universe.files and not base.endswith(".d.ts"):
        return base
    for ext in extensions:
        if f"{base}{ext}" in universe.files:
            return f"{base}{ext}"
    for ext in extensions:
        door = f"{base}/index{ext}"
        if door in universe.files:
            return door
    return None


def _resolve_specifier(
    src_path: str, spec: str, universe: Universe, config: TsConfig
) -> tuple[str | None, str | None]:
    clean = spec.split("?", 1)[0].split("#", 1)[0]
    for prefix, target in config.aliases:
        if clean.startswith(prefix):
            rel = posixpath.normpath(f"{target}{clean[len(prefix) :]}")
            return _resolve_candidate(rel, universe, config.extensions), None
    if clean.startswith("."):
        base_dir = parent_of(src_path)
        joined = f"{base_dir}/{clean}" if base_dir else clean
        rel = posixpath.normpath(joined)
        if rel.startswith(".."):
            return None, None
        return _resolve_candidate(rel, universe, config.extensions), None
    parts = clean.split("/")
    pkg = "/".join(parts[:2]) if clean.startswith("@") and len(parts) >= 2 else parts[0]
    return None, pkg


def _clause_names(clause: str) -> tuple[str, ...]:
    names: list[str] = []
    brace = re.search(r"\{([^}]*)\}", clause)
    if brace is not None:
        for item in brace.group(1).split(","):
            cleaned = item.strip().removeprefix("type ").strip()
            orig = cleaned.split(" as ", 1)[0].strip()
            if orig and orig != "*":
                names.append(orig)
    return tuple(names)


def _collect_imports(
    path: str,
    uncommented: str,
    universe: Universe,
    config: TsConfig,
    *,
    full_scan: bool,
) -> tuple[list[Fact], set[str], dict[str, str]]:
    facts: list[Fact] = []
    external: set[str] = set()
    local_bindings: dict[str, str] = {}
    seen: set[tuple[int, str, tuple[str, ...]]] = set()

    def record(spec: str, offset: int, names: tuple[str, ...], detail: str) -> str | None:
        dst, ext = _resolve_specifier(path, spec, universe, config)
        if dst is not None and dst != path:
            if not config.scopes or _under_any(dst, config.scopes):
                line = _line_of(uncommented, offset)
                fact_names = names if full_scan else ()
                key = (line, dst, fact_names)
                if key not in seen:
                    seen.add(key)
                    facts.append(Fact(path, dst, line, FactKind.IMPORT, fact_names, detail))
            return dst
        if ext is not None and full_scan:
            external.add(ext)
        return None

    for match in _IMPORT_FROM_RE.finditer(uncommented):
        kind, clause, spec = match.group(1), match.group(2), match.group(3)
        if "\nimport " in clause or "\nexport " in clause:
            continue
        names = _clause_names(clause)
        dst = record(spec, match.start(), names, f"{kind} from {spec!r}")
        if dst is not None and kind == "import":
            _bind_clause(clause, dst, local_bindings)
    for match in _SIDE_EFFECT_IMPORT_RE.finditer(uncommented):
        spec = match.group(1)
        record(spec, match.start(), (), f"import {spec!r}")
    for match in _DYNAMIC_IMPORT_RE.finditer(uncommented):
        spec = match.group(1)
        record(spec, match.start(), (), f"import({spec!r})")
    for match in _VI_MOCK_RE.finditer(uncommented):
        form, spec = match.group(1), match.group(2)
        record(spec, match.start(), (), f"{form}({spec!r})")
    return facts, external, local_bindings


def _bind_clause(clause: str, dst: str, bindings: dict[str, str]) -> None:
    ns_match = re.search(r"\*\s+as\s+([A-Za-z_$][\w$]*)", clause)
    if ns_match is not None:
        bindings[ns_match.group(1)] = dst
    brace = re.search(r"\{([^}]*)\}", clause)
    if brace is not None:
        for item in brace.group(1).split(","):
            cleaned = item.strip().removeprefix("type ").strip()
            if not cleaned:
                continue
            parts = [p.strip() for p in cleaned.split(" as ", 1)]
            local = parts[-1]
            if local:
                bindings[local] = dst


def _collect_patches(
    path: str,
    uncommented: str,
    universe: Universe,
    config: TsConfig,
    bindings: dict[str, str],
) -> list[Patch]:
    patches: list[Patch] = []
    for match in _VI_MOCK_RE.finditer(uncommented):
        form, spec = match.group(1), match.group(2)
        line = _line_of(uncommented, match.start())
        dst, ext = _resolve_specifier(path, spec, universe, config)
        if dst is not None:
            patches.append(Patch(line, form, dst, Code.FIRST_PARTY_PATCH))
        elif ext is not None:
            patches.append(Patch(line, form, ext, Code.THIRD_PARTY_PATCH))
    for match in _VI_SPY_RE.finditer(uncommented):
        form, obj, method = match.group(1), match.group(2), match.group(3)
        line = _line_of(uncommented, match.start())
        root = obj.split(".", 1)[0]
        target = f"{obj}.{method}"
        if root in bindings:
            patches.append(Patch(line, form, f"{bindings[root]}.{method}", Code.FIRST_PARTY_PATCH))
        else:
            patches.append(Patch(line, form, target, Code.STDLIB_PATCH))
    for match in _VI_STUB_ENV_RE.finditer(uncommented):
        line = _line_of(uncommented, match.start())
        patches.append(Patch(line, match.group(1), match.group(2), Code.ENV_PATCH))
    for match in _VI_STUB_GLOBAL_RE.finditer(uncommented):
        line = _line_of(uncommented, match.start())
        patches.append(Patch(line, match.group(1), match.group(2), Code.STDLIB_PATCH))
    return patches


def _collect_exports(code_only: str) -> tuple[frozenset[str], frozenset[str]]:
    runtime_exports: set[str] = set()
    all_defined: set[str] = set()
    for match in _EXPORT_DECL_RE.finditer(code_only):
        kind, name = match.group(1), match.group(2)
        all_defined.add(name)
        if kind not in {"interface", "type"}:
            runtime_exports.add(name)
    for match in _EXPORT_LIST_RE.finditer(code_only):
        line_end = code_only.find("\n", match.end())
        rest = code_only[match.end() : line_end if line_end != -1 else len(code_only)]
        is_type_list = match.group(0).lstrip().startswith("export type")
        for item in match.group(1).split(","):
            raw = item.strip()
            if not raw:
                continue
            is_type_item = is_type_list or raw.startswith("type ")
            cleaned = raw.removeprefix("type ").strip()
            exported = cleaned.split(" as ", 1)[-1].strip()
            if exported and exported != "*":
                all_defined.add(exported)
                if not is_type_item and not re.match(r"^\s*from\b", rest):
                    runtime_exports.add(exported)
    return frozenset(runtime_exports), frozenset(all_defined)


def _split_top_level_commas(text: str) -> list[str]:
    items: list[str] = []
    depth = 0
    start = 0
    for idx, ch in enumerate(text):
        if ch in "([{<":
            depth += 1
        elif ch in ")]}>":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            part = text[start:idx].strip()
            if part:
                items.append(part)
            start = idx + 1
    tail = text[start:].strip()
    if tail:
        items.append(tail)
    return items


def _matching_delim(text: str, open_pos: int, open_ch: str, close_ch: str) -> int:
    depth = 0
    for idx in range(open_pos, len(text)):
        ch = text[idx]
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return idx
    return -1


def _find_body_brace(code_only: str, close_paren: int) -> int:
    angle = 0
    paren = 0
    for idx in range(close_paren + 1, len(code_only)):
        ch = code_only[idx]
        if ch == "<":
            angle += 1
        elif ch == ">":
            angle = max(0, angle - 1)
        elif ch == "(":
            paren += 1
        elif ch == ")":
            paren = max(0, paren - 1)
        elif angle == 0 and paren == 0:
            if ch == "{":
                return idx
            if ch == ";":
                return -1
    return -1


def _param_ident(param: str) -> str | None:
    lhs = (
        param.split(":", 1)[0]
        .split("=", 1)[0]
        .strip()
        .removeprefix("...")
        .removesuffix("?")
        .strip()
    )
    return lhs if re.fullmatch(r"[A-Za-z_$][\w$]*", lhs) else None


def _check_passthrough(name: str, line: int, params: list[str], body: str) -> PurityItem | None:
    ident_params = [_param_ident(p) for p in params]
    if not ident_params or any(p is None for p in ident_params):
        return None
    compact = " ".join(body.split())
    match = _PASSTHROUGH_BODY_RE.match(compact)
    if match is None:
        return None
    callee = match.group(1)
    args = [a.strip() for a in _split_top_level_commas(match.group(2))]
    if args != ident_params or callee.split(".")[-1] == name:
        return None
    return PurityItem(
        line,
        Code.PASSTHROUGH_FUNCTION,
        name,
        f"`{name}` is a pass-through wrapper around `{callee}`",
        "call the target directly or give the wrapper a real domain responsibility",
    )


def _scan_functions(
    code_only: str, config: TsConfig, *, test_file: bool
) -> tuple[list[PurityItem], list[tuple[int, int, str]]]:
    items: list[PurityItem] = []
    spans: list[tuple[int, int, str]] = []
    for match in _TOP_FUNC_RE.finditer(code_only):
        name = match.group(2)
        line = _line_of(code_only, match.start())
        open_paren = match.end() - 1
        close_paren = _matching_delim(code_only, open_paren, "(", ")")
        if close_paren == -1:
            continue
        params = _split_top_level_commas(code_only[open_paren + 1 : close_paren])
        if not test_file and len(params) > config.max_params:
            items.append(
                PurityItem(
                    line,
                    Code.WIDE_PARAMS,
                    name,
                    f"`{name}` takes {len(params)} parameters (max {config.max_params})",
                    "group related parameters into a cohesive options/value interface",
                )
            )
        open_brace = _find_body_brace(code_only, close_paren)
        if open_brace == -1:
            continue
        close_brace = _matching_delim(code_only, open_brace, "{", "}")
        if close_brace == -1:
            continue
        spans.append((match.start(), close_brace + 1, name))
        if not test_file:
            pt = _check_passthrough(name, line, params, code_only[open_brace : close_brace + 1])
            if pt is not None:
                items.append(pt)
    return items, spans


def _enclosing_symbol(offset: int, spans: list[tuple[int, int, str]]) -> str:
    for start, end, name in spans:
        if start <= offset < end:
            return name
    return "<module>"


def _scan_impure_calls(
    code_only: str,
    config: TsConfig,
    spans: list[tuple[int, int, str]],
) -> list[PurityItem]:
    items: list[PurityItem] = []
    seen: set[tuple[str, str]] = set()

    def add(offset: int, callee: str) -> None:
        owner = _enclosing_symbol(offset, spans)
        key = f"{owner}#{callee}"
        if (owner, callee) not in seen:
            seen.add((owner, callee))
            line = _line_of(code_only, offset)
            items.append(
                PurityItem(
                    line,
                    Code.IMPURE_CALL,
                    key,
                    f"`{owner}` performs impure call or side effect `{callee}`",
                    "move I/O, timers, DOM/global access, and stateful hooks out of strict pure modules",
                )
            )

    for match in _MEMBER_CALL_RE.finditer(code_only):
        obj, prop = match.group(1), match.group(2)
        pair = f"{obj}.{prop}"
        if pair in config.impure_calls:
            add(match.start(), pair)
        elif obj in config.impure_calls:
            add(match.start(), obj)
    for match in _NEW_CALL_RE.finditer(code_only):
        ctor = match.group(1)
        if ctor in config.impure_calls:
            add(match.start(), f"new {ctor}")
        elif ctor == "Date":
            close = _matching_delim(code_only, match.end() - 1, "(", ")")
            if close != -1 and not code_only[match.end() : close].strip():
                add(match.start(), "new Date()")
    for match in _DIRECT_CALL_RE.finditer(code_only):
        callee = match.group(1)
        if callee in config.impure_calls or callee in config.impure_hooks:
            add(match.start(), callee)
    return items


def _scan_purity(
    path: str,
    code_only: str,
    config: TsConfig,
    runtime_exports: frozenset[str],
    *,
    test_file: bool,
) -> list[PurityItem]:
    items, spans = _scan_functions(code_only, config, test_file=test_file)
    if not test_file:
        for match in _TOP_MUTABLE_RE.finditer(code_only):
            name = match.group(1)
            line = _line_of(code_only, match.start())
            items.append(
                PurityItem(
                    line,
                    Code.UNFROZEN_VALUE,
                    name,
                    f"module-level `{name}` is mutable (`let`/`var`)",
                    "replace module-level mutable state with `const` or an explicit store",
                )
            )
        for match in _TOP_COLLECTION_RE.finditer(code_only):
            name = match.group(1)
            line = _line_of(code_only, match.start())
            items.append(
                PurityItem(
                    line,
                    Code.UNFROZEN_VALUE,
                    name,
                    f"module-level `{name}` is a mutable collection (`Map`/`Set`)",
                    "keep mutable caches or registries out of strict pure modules",
                )
            )
        is_door = path.rsplit("/", 1)[-1] in Universe.DOOR_FILES
        if not is_door and len(runtime_exports) > config.max_public_symbols:
            items.append(
                PurityItem(
                    1,
                    Code.WIDE_PUBLIC_SURFACE,
                    "__all__",
                    f"{path} exports {len(runtime_exports)} runtime symbols (max {config.max_public_symbols})",
                    "split cohesive responsibilities into focused modules or narrow the exported surface",
                )
            )
    items.extend(_scan_impure_calls(code_only, config, spans))
    return items


def scan_typescript(
    path: str,
    source: str,
    universe: Universe,
    policy: Policy | None = None,
) -> TsScan:
    loaded = allowances.load(universe.root) if policy is None else policy
    config = load_ts_config(loaded)
    full_scan = not config.scopes or _under_any(path, config.scopes)
    uncommented, code_only = _strip_comments_and_strings(source)
    facts, external, bindings = _collect_imports(
        path, uncommented, universe, config, full_scan=full_scan
    )
    if not full_scan:
        return TsScan(path, tuple(facts), frozenset(), (), (), (), None, frozenset())
    test_file = fixtures.is_test_file(path)
    patches = _collect_patches(path, uncommented, universe, config, bindings) if test_file else []
    runtime_exports, all_defined = _collect_exports(code_only)
    purity = _scan_purity(path, code_only, config, runtime_exports, test_file=test_file)
    return TsScan(
        path=path,
        facts=tuple(facts),
        external=frozenset(external),
        patches=tuple(patches),
        purity=tuple(purity),
        problems=(),
        exports_all=None if test_file else runtime_exports,
        defined=all_defined,
    )
