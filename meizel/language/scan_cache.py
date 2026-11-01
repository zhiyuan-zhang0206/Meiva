# meiva:note
# Content-addressed JSON serialization and SQLite-backed caching for `FileScan` and `Sources`.
# meiva:link meizel/docs/checker/checker.meiva.okf.md

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from typing import Literal, TypeGuard

from meizel.allowances import POLICY, Policy
from meizel.graph.model import (
    Acquisition,
    AmbientKind,
    AnyCollaboratorSite,
    Code,
    EntropySite,
    EnumSubsetUse,
    Fact,
    FactKind,
    Patch,
    Problem,
    PurityItem,
    Restricted,
    Sources,
    TypeCastSite,
    Unless,
)
from meizel.graph.universe import MANIFEST, Universe
from meizel.language.fixtures import FixtureFunction, FixtureRequest, FixtureState
from meizel.language.input_mutations import InputListAppend
from meizel.language.scan import FileScan, scan_file, scan_typescript_files
from meizel.language.typescript import load_ts_config, typescript_files

__all__ = [
    "cached_module_imports",
    "decode_file_scan",
    "encode_file_scan",
    "load_cached_sources",
    "scan_all",
    "store_cached_sources",
]

_PARALLEL_FROM = 200
_MISSING = "<missing>"
_TS_WORLD_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".json")
_COMMON_SCAN_KEYS = frozenset({"path", "facts", "imported", "symbols", "type_checking", "deps"})
_FACT_KINDS: dict[str, FactKind] = {k.value: k for k in FactKind}
_CODES: dict[str, Code] = {c.value: c for c in Code}
_AMBIENT_KINDS: dict[str, AmbientKind] = {k.value: k for k in AmbientKind}
_WORKER: dict[str, tuple[Universe, Sources, Policy]] = {}
type _FixtureForm = Literal["parameter", "usefixtures", "call", "lookup"]


@dataclass(frozen=True, slots=True)
class _CachedPyScan:
    scan: FileScan
    deps: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class _CachedTsBatch:
    scans: dict[str, FileScan]


@dataclass(frozen=True, slots=True)
class _CachedImports:
    imports: dict[str, str]


def _is_object(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def _is_array(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_str_list(value: object) -> TypeGuard[list[str]]:
    return _is_array(value) and all(isinstance(x, str) for x in value)


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("expected string")
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return _text(value)


def _integer(value: object) -> int:
    if type(value) is not int:
        raise TypeError("expected int")
    return value


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return _integer(value)


def _flag(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("expected bool")
    return value


def _array(value: object) -> list[object]:
    if not _is_array(value):
        raise TypeError("expected list")
    return value


def _row(value: object, size: int) -> list[object]:
    items = _array(value)
    if len(items) != size:
        raise ValueError(f"expected {size} elements")
    return items


def _record(value: object) -> dict[str, object]:
    if not _is_object(value):
        raise TypeError("expected object")
    return value


def _fixture_form(value: object) -> _FixtureForm:
    text = _text(value)
    if text == "parameter":
        return "parameter"
    if text == "usefixtures":
        return "usefixtures"
    if text == "call":
        return "call"
    if text == "lookup":
        return "lookup"
    raise ValueError(f"unknown fixture form: {text}")


def _put_if(out: dict[str, object], key: str, value: Sequence[object]) -> None:
    if value:
        out[key] = list(value)


def _encode_fixtures_raw(fx: FixtureState) -> dict[str, object]:
    out: dict[str, object] = {}
    _put_if(out, "defs", sorted(fx.defs))
    _put_if(
        out,
        "requests",
        [[r.name, r.line, r.skip_self, r.requester_line, r.form] for r in fx.requests],
    )
    _put_if(out, "plugins", [[name, line] for name, line in fx.plugins])
    _put_if(
        out,
        "functions",
        [
            [fn.name, fn.line, fn.registered, fn.module_owned, fn.parameters_known]
            for fn in fx.functions
        ],
    )
    return out


def _scan_to_raw_edges(scan: FileScan, out: dict[str, object]) -> None:
    _put_if(
        out,
        "facts",
        [[f.src, f.dst, f.line, f.kind.value, list(f.names), f.detail] for f in scan.facts],
    )
    _put_if(out, "restricted", [[r.line, r.form, r.detail] for r in scan.restricted])
    _put_if(
        out,
        "patches",
        [[p.line, p.form, p.target, p.code.value, p.imported] for p in scan.patches],
    )
    _put_if(out, "problems", [[p.line, p.code.value, p.message] for p in scan.problems])
    fx_raw = _encode_fixtures_raw(scan.fixtures)
    if fx_raw:
        out["fixtures"] = fx_raw
    _put_if(out, "imported", sorted(scan.imported))


def _scan_to_raw_sites(scan: FileScan, out: dict[str, object]) -> None:
    _put_if(
        out,
        "purity",
        [[p.line, p.code.value, p.key, p.message, p.hint] for p in scan.purity],
    )
    _put_if(
        out,
        "enum_subsets",
        [[u.line, u.cls_name, list(u.members)] for u in scan.enum_subsets],
    )
    _put_if(
        out,
        "input_appends",
        [[a.line, a.function, a.parameter] for a in scan.input_appends],
    )
    _put_if(out, "ambient", [[a.line, a.kind.value, a.target] for a in scan.ambient])
    _put_if(out, "entropy", [[e.line, e.what] for e in scan.entropy])
    _put_if(out, "type_casts", [[c.line, c.column, c.qualname] for c in scan.type_casts])
    _put_if(
        out,
        "any_collaborators",
        [[c.line, c.column, c.qualname, c.slot, c.role] for c in scan.any_collaborators],
    )
    _put_if(out, "type_checking", list(scan.type_checking))
    _put_if(out, "symbols", [[s[0], s[1], s[2]] for s in scan.symbols])
    _put_if(out, "deps", list(scan.deps))


def _scan_to_raw(scan: FileScan) -> dict[str, object]:
    out: dict[str, object] = {"path": scan.path}
    _scan_to_raw_edges(scan, out)
    _scan_to_raw_sites(scan, out)
    return out


def _opt_list(raw: Mapping[str, object], key: str) -> list[object]:
    return _array(raw[key]) if key in raw else []


def _opt_rows(raw: Mapping[str, object], key: str, size: int) -> list[list[object]]:
    if key not in raw:
        return []
    return [_row(item, size) for item in _array(raw[key])]


def _decode_fixtures(raw: Mapping[str, object]) -> FixtureState:
    if "fixtures" not in raw:
        return FixtureState()
    fx = _record(raw["fixtures"])
    defs = {_text(item) for item in _opt_list(fx, "defs")}
    requests = [
        FixtureRequest(
            _text(name),
            _integer(line),
            skip_self=_flag(skip_self),
            requester_line=_integer(req_line),
            form=_fixture_form(form),
        )
        for name, line, skip_self, req_line, form in _opt_rows(fx, "requests", 5)
    ]
    plugins = [(_text(name), _integer(line)) for name, line in _opt_rows(fx, "plugins", 2)]
    functions = [
        FixtureFunction(
            _text(name), _integer(line), _optional_text(reg), _flag(owned), _flag(known)
        )
        for name, line, reg, owned, known in _opt_rows(fx, "functions", 5)
    ]
    return FixtureState(defs=defs, requests=requests, plugins=plugins, functions=functions)


def _fact_from_row(item: object) -> Fact:
    src, dst, line, kind, names, detail = _row(item, 6)
    if (
        isinstance(src, str)
        and isinstance(dst, str)
        and type(line) is int
        and isinstance(kind, str)
        and _is_str_list(names)
        and isinstance(detail, str)
    ):
        return Fact(src, dst, line, _FACT_KINDS[kind], tuple(names), detail)
    raise TypeError("malformed fact row")


def _decode_facts(raw: Mapping[str, object]) -> list[Fact]:
    if "facts" not in raw:
        return []
    return [_fact_from_row(item) for item in _array(raw["facts"])]


def _decode_patches(raw: Mapping[str, object]) -> list[Patch]:
    return [
        Patch(_integer(line), _text(form), _text(target), _CODES[_text(code)], imported=_flag(imp))
        for line, form, target, code, imp in _opt_rows(raw, "patches", 5)
    ]


def _decode_purity(raw: Mapping[str, object]) -> list[PurityItem]:
    return [
        PurityItem(_integer(line), _CODES[_text(code)], _text(key), _text(msg), _text(hint))
        for line, code, key, msg, hint in _opt_rows(raw, "purity", 5)
    ]


def _decode_collaborators(raw: Mapping[str, object]) -> list[AnyCollaboratorSite]:
    return [
        AnyCollaboratorSite(_integer(line), _integer(col), _text(qual), _text(slot), _text(role))
        for line, col, qual, slot, role in _opt_rows(raw, "any_collaborators", 5)
    ]


def _symbol_from_row(item: object) -> tuple[str, int, int]:
    name, start, end = _row(item, 3)
    if isinstance(name, str) and type(start) is int and type(end) is int:
        return (name, start, end)
    raise TypeError("malformed symbol row")


def _decode_symbols(raw: Mapping[str, object]) -> tuple[tuple[str, int, int], ...]:
    if "symbols" not in raw:
        return ()
    return tuple(_symbol_from_row(item) for item in _array(raw["symbols"]))


def _decode_common_scan(raw: Mapping[str, object]) -> FileScan:
    return FileScan(
        _text(raw["path"]),
        facts=_decode_facts(raw),
        imported={_text(item) for item in _opt_list(raw, "imported")},
        type_checking=tuple(_integer(item) for item in _opt_list(raw, "type_checking")),
        symbols=_decode_symbols(raw),
        deps=tuple(_text(item) for item in _opt_list(raw, "deps")),
    )


def _scan_from_raw(value: object) -> FileScan:
    raw = _record(value)
    if raw.keys() <= _COMMON_SCAN_KEYS:
        return _decode_common_scan(raw)
    return FileScan(
        _text(raw["path"]),
        facts=_decode_facts(raw),
        restricted=[
            Restricted(_integer(line), _text(form), _text(detail))
            for line, form, detail in _opt_rows(raw, "restricted", 3)
        ],
        patches=_decode_patches(raw),
        problems=[
            Problem(_integer(line), _CODES[_text(code)], _text(msg))
            for line, code, msg in _opt_rows(raw, "problems", 3)
        ],
        fixtures=_decode_fixtures(raw),
        imported={_text(item) for item in _opt_list(raw, "imported")},
        purity=_decode_purity(raw),
        enum_subsets=[
            EnumSubsetUse(_integer(line), _text(cls), tuple(_text(m) for m in _array(members)))
            for line, cls, members in _opt_rows(raw, "enum_subsets", 3)
        ],
        input_appends=[
            InputListAppend(_integer(line), _text(fn), _text(param))
            for line, fn, param in _opt_rows(raw, "input_appends", 3)
        ],
        ambient=[
            Acquisition(_integer(line), _AMBIENT_KINDS[_text(kind)], _text(target))
            for line, kind, target in _opt_rows(raw, "ambient", 3)
        ],
        entropy=[
            EntropySite(_integer(line), _text(what)) for line, what in _opt_rows(raw, "entropy", 2)
        ],
        type_casts=[
            TypeCastSite(_integer(line), _integer(col), _text(qual))
            for line, col, qual in _opt_rows(raw, "type_casts", 3)
        ],
        any_collaborators=_decode_collaborators(raw),
        type_checking=tuple(_integer(item) for item in _opt_list(raw, "type_checking")),
        symbols=_decode_symbols(raw),
        deps=tuple(_text(item) for item in _opt_list(raw, "deps")),
    )


def encode_file_scan(scan: FileScan) -> str:
    return json.dumps(_scan_to_raw(scan), separators=(",", ":"))


def decode_file_scan(text: str) -> FileScan:
    raw: object = json.loads(text)
    return _scan_from_raw(raw)


def _sources_to_json(sources: Sources) -> str:
    payload = {
        "names": [[k, v.value] for k, v in sorted(sources.names.items())],
        "calls": [[k, v.value] for k, v in sorted(sources.calls.items())],
        "uses": sorted(sources.uses),
        "unless": [[k, u.parameter, u.position] for k, u in sorted(sources.unless.items())],
    }
    return json.dumps(payload, separators=(",", ":"))


def _sources_from_json(text: str) -> Sources:
    raw_obj: object = json.loads(text)
    raw = _record(raw_obj)
    names = {
        _text(k): AmbientKind(_text(v)) for k, v in (_row(item, 2) for item in _array(raw["names"]))
    }
    calls = {
        _text(k): AmbientKind(_text(v)) for k, v in (_row(item, 2) for item in _array(raw["calls"]))
    }
    uses = frozenset(_text(item) for item in _array(raw["uses"]))
    unless = {
        _text(k): Unless(_text(param), _optional_int(pos))
        for k, param, pos in (_row(item, 3) for item in _array(raw["unless"]))
    }
    return Sources(names=names, calls=calls, uses=uses, unless=unless)


def _policy_digest(policy: Policy) -> str:
    payload = {
        "external": sorted(policy.external.items()),
        "loads": [[k, list(v)] for k, v in sorted(policy.loads.items())],
        "defaults": list(policy.default_arguments),
        "listed": [[r, s, sorted(v.items())] for (r, s), v in sorted(policy.listed.items())],
        "named": [[r, s, list(v)] for (r, s), v in sorted(policy.named.items())],
        "unless": [[r, s, sorted(v.items())] for (r, s), v in sorted(policy.unless.items())],
    }
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def _projects_digest(universe: Universe) -> str:
    payload = [
        [item.directory, [[name, target] for name, target in item.packages]]
        for item in universe.standalone_projects
    ]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def load_cached_sources(
    universe: Universe, state_digest: str, policy: Policy
) -> tuple[str, Sources | None]:
    digests = universe.file_digests()
    if not digests or any(path not in digests for path in universe.python_files):
        return "", None
    repo = universe.repo
    hasher = hashlib.sha256()
    hasher.update(
        f"{repo.scanner_digest()}\0{state_digest}\0{_policy_digest(policy)}\0{_projects_digest(universe)}\0".encode()
    )
    for path in universe.python_files:
        hasher.update(f"{path}:{digests[path]}\0".encode())
    key = f"sources:v1:{hasher.hexdigest()}"
    memo = repo.memo_get(key)
    if isinstance(memo, Sources):
        return key, memo
    hit = repo.cache_get_many((key,)).get(key)
    if hit is None:
        return key, None
    try:
        decoded = _sources_from_json(hit)
    except (ValueError, TypeError, KeyError, IndexError):
        return key, None
    repo.memo_put(key, decoded)
    return key, decoded


def store_cached_sources(universe: Universe, cache_key: str, sources: Sources) -> None:
    if cache_key:
        universe.repo.memo_put(cache_key, sources)
        universe.repo.cache_put_many({cache_key: _sources_to_json(sources)})


def _decode_imports_json(hit: str) -> dict[str, str] | None:
    try:
        raw: object = json.loads(hit)
        return {_text(k): _text(v) for k, v in _record(raw).items()}
    except (ValueError, TypeError, KeyError):
        return None


def _partition_memo_imports(
    universe: Universe, keys: Mapping[str, str]
) -> tuple[dict[str, dict[str, str]], list[str]]:
    found: dict[str, dict[str, str]] = {}
    pending: list[str] = []
    for path, key in keys.items():
        memo = universe.repo.memo_get(key)
        if isinstance(memo, _CachedImports):
            found[path] = dict(memo.imports)
        else:
            pending.append(path)
    return found, pending


def cached_module_imports(
    universe: Universe,
    paths: Collection[str],
    compute: Callable[[Universe, str], dict[str, str]],
) -> dict[str, dict[str, str]]:
    ordered = sorted(paths)
    if not ordered:
        return {}
    digests = universe.file_digests()
    if not digests or any(path not in digests for path in ordered):
        return {path: compute(universe, path) for path in ordered}
    repo = universe.repo
    prefix = f"pyimp:v1:{repo.scanner_digest()}:{_projects_digest(universe)}"
    keys = {path: f"{prefix}:{path}:{digests[path]}" for path in ordered}
    found, pending = _partition_memo_imports(universe, keys)
    if not pending:
        return found
    cached = repo.cache_get_many([keys[p] for p in pending])
    updates: dict[str, str] = {}
    for path in pending:
        key = keys[path]
        hit = cached.get(key)
        decoded = None if hit is None else _decode_imports_json(hit)
        if decoded is None:
            decoded = compute(universe, path)
            updates[key] = json.dumps(decoded, separators=(",", ":"))
        repo.memo_put(key, _CachedImports(decoded))
        found[path] = decoded
    if updates:
        repo.cache_put_many(updates)
    return found


def _py_context_digest(
    universe: Universe, sources: Sources, policy: Policy, digests: Mapping[str, str]
) -> str:
    hasher = hashlib.sha256()
    repo = universe.repo
    policy_present = POLICY in universe.files
    policy_blob = digests.get(POLICY, _MISSING) if policy_present else _MISSING
    hasher.update(
        (
            f"{repo.scanner_digest()}\0{_policy_digest(policy)}\0"
            f"{policy_present}:{policy_blob}\0{_sources_to_json(sources)}\0"
            f"{_projects_digest(universe)}\0"
        ).encode()
    )
    for path in universe.python_files:
        hasher.update(f"p:{path}\0".encode())
    for directory in sorted(universe.dirs):
        hasher.update(f"d:{directory}\0".encode())
    for root_name in sorted(universe.roots):
        hasher.update(f"r:{root_name}\0".encode())
    return hasher.hexdigest()


def _deps_valid(deps: tuple[tuple[str, str], ...], digests: Mapping[str, str]) -> bool:
    return all(digests.get(dep, _MISSING) == expected for dep, expected in deps)


def _decode_py_entry(text: str, digests: Mapping[str, str]) -> _CachedPyScan | None:
    try:
        raw_obj: object = json.loads(text)
        entry = _record(raw_obj)
        deps = tuple(
            (_text(dep), _text(expected))
            for dep, expected in (_row(pair, 2) for pair in _opt_list(entry, "deps"))
        )
        if not _deps_valid(deps, digests):
            return None
        return _CachedPyScan(_scan_from_raw(entry["scan"]), deps)
    except (ValueError, TypeError, KeyError, IndexError):
        return None


def _encode_py_entry(
    scan: FileScan, raw_scan: dict[str, object], digests: Mapping[str, str]
) -> str:
    payload: dict[str, object] = {"scan": raw_scan}
    if scan.deps:
        payload["deps"] = [[dep, digests.get(dep, _MISSING)] for dep in scan.deps]
    return json.dumps(payload, separators=(",", ":"))


def _init_worker(universe: Universe, sources: Sources, policy: Policy) -> None:
    _WORKER["scan"] = (universe, sources, policy)


def _scan_in_worker(path: str) -> tuple[FileScan, str]:
    universe, sources, policy = _WORKER["scan"]
    scan = scan_file(universe, path, sources, policy=policy)
    return scan, encode_file_scan(scan)


def _scan_python_misses(
    universe: Universe, sources: Sources, paths: list[str], jobs: int | None, policy: Policy
) -> list[tuple[FileScan, dict[str, object]]]:
    workers = jobs if jobs is not None else min(os.cpu_count() or 1, 8)
    if workers <= 1 or len(paths) < _PARALLEL_FROM:
        return [
            (scan, _scan_to_raw(scan))
            for scan in (scan_file(universe, p, sources, policy=policy) for p in paths)
        ]
    with ProcessPoolExecutor(
        workers, initializer=_init_worker, initargs=(universe, sources, policy)
    ) as pool:
        chunk = max(1, len(paths) // (workers * 8))
        pairs = list(pool.map(_scan_in_worker, paths, chunksize=chunk))
    out: list[tuple[FileScan, dict[str, object]]] = []
    for scan, text in pairs:
        raw: object = json.loads(text)
        out.append((scan, _record(raw)))
    return out


def _from_py_memo(
    universe: Universe,
    paths: list[str],
    keys: Mapping[str, str],
    digests: Mapping[str, str],
) -> tuple[dict[str, FileScan], list[str]]:
    scans: dict[str, FileScan] = {}
    pending: list[str] = []
    for path in paths:
        memo = universe.repo.memo_get(keys[path]) if path in keys else None
        if isinstance(memo, _CachedPyScan) and _deps_valid(memo.deps, digests):
            scans[path] = memo.scan
        else:
            pending.append(path)
    return scans, pending


def _partition_py_cache(
    universe: Universe, paths: list[str], ctx: str, digests: Mapping[str, str]
) -> tuple[dict[str, str], dict[str, FileScan], list[str]]:
    keys = {p: f"pyscan:v1:{ctx}:{p}:{digests[p]}" for p in paths if p in digests}
    scans, pending = _from_py_memo(universe, paths, keys, digests)
    db_keys = [keys[p] for p in pending if p in keys]
    cached = universe.repo.cache_get_many(db_keys) if db_keys else {}
    misses: list[str] = []
    for path in pending:
        hit = cached.get(keys[path]) if path in keys else None
        decoded = None if hit is None else _decode_py_entry(hit, digests)
        if decoded is not None and path in keys:
            universe.repo.memo_put(keys[path], decoded)
            scans[path] = decoded.scan
        else:
            misses.append(path)
    return keys, scans, misses


def _store_py_scans(
    universe: Universe,
    fresh: list[tuple[FileScan, dict[str, object]]],
    keys: Mapping[str, str],
    digests: Mapping[str, str],
) -> None:
    updates: dict[str, str] = {}
    for scan, raw_scan in fresh:
        if scan.path not in keys or any(dep not in digests for dep in scan.deps):
            continue
        key = keys[scan.path]
        deps = tuple((dep, digests[dep]) for dep in scan.deps)
        universe.repo.memo_put(key, _CachedPyScan(scan, deps))
        updates[key] = _encode_py_entry(scan, raw_scan, digests)
    if updates:
        universe.repo.cache_put_many(updates)


def _scan_python_cached(
    universe: Universe,
    sources: Sources,
    paths: list[str],
    jobs: int | None,
    policy: Policy,
) -> dict[str, FileScan]:
    if not paths:
        return {}
    digests = universe.file_digests()
    if not digests:
        return {
            scan.path: scan
            for scan, _ in _scan_python_misses(universe, sources, paths, jobs, policy)
        }
    ctx = _py_context_digest(universe, sources, policy, digests)
    keys, scans, misses = _partition_py_cache(universe, paths, ctx, digests)
    if not misses:
        return scans
    fresh = _scan_python_misses(universe, sources, misses, jobs, policy)
    for scan, _ in fresh:
        scans[scan.path] = scan
    _store_py_scans(universe, fresh, keys, digests)
    return scans


def _ts_batch_key(
    universe: Universe,
    paths: list[str],
    sources: Sources,
    policy: Policy,
    digests: Mapping[str, str],
) -> str:
    if not digests or any(path not in digests for path in paths):
        return ""
    non_manifest = [p for p in sorted(universe.files) if p.rsplit("/", 1)[-1] != MANIFEST]
    world_files = [p for p in non_manifest if p.endswith(_TS_WORLD_SUFFIXES)]
    if any(p not in digests for p in world_files):
        return ""
    cfg = load_ts_config(policy)
    payload = {
        "scanner": universe.repo.scanner_digest(),
        "paths": paths,
        "config": {
            "scopes": list(cfg.scopes),
            "import_sources": list(cfg.import_sources),
            "aliases": [[a, b] for a, b in cfg.aliases],
            "extensions": list(cfg.extensions),
            "impure_calls": sorted(cfg.impure_calls),
            "impure_hooks": sorted(cfg.impure_hooks),
            "max_public_symbols": cfg.max_public_symbols,
            "max_params": cfg.max_params,
            "max_attr_hops": cfg.max_attr_hops,
            "inlined_env_prefix": cfg.inlined_env_prefix,
            "inlined_env_roots": list(cfg.inlined_env_roots),
        },
        "ambient": {
            "names": sorted((k, v.value) for k, v in sources.names.items()),
            "calls": sorted((k, v.value) for k, v in sources.calls.items()),
        },
        "world": [[p, digests[p]] for p in world_files],
    }
    digest = hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()
    return f"tsbatch:v1:{digest}"


def _scan_ts_cached(
    universe: Universe, paths: list[str], sources: Sources, policy: Policy
) -> dict[str, FileScan]:
    if not paths:
        return {}
    digests = universe.file_digests()
    key = _ts_batch_key(universe, paths, sources, policy, digests)
    if not key:
        return scan_typescript_files(universe, paths, sources, policy)
    repo = universe.repo
    memo = repo.memo_get(key)
    if isinstance(memo, _CachedTsBatch) and set(memo.scans) == set(paths):
        return dict(memo.scans)
    hit = repo.cache_get_many((key,)).get(key)
    if hit is not None:
        try:
            raw_obj: object = json.loads(hit)
            decoded = {s.path: s for s in (_scan_from_raw(item) for item in _array(raw_obj))}
            if set(decoded) == set(paths):
                repo.memo_put(key, _CachedTsBatch(decoded))
                return decoded
        except (ValueError, TypeError, KeyError, IndexError):
            decoded = {}
    scans = scan_typescript_files(universe, paths, sources, policy)
    repo.memo_put(key, _CachedTsBatch(dict(scans)))
    encoded = json.dumps([_scan_to_raw(scans[p]) for p in paths], separators=(",", ":"))
    repo.cache_put_many({key: encoded})
    return scans


def scan_all(
    universe: Universe,
    sources: Sources,
    jobs: int | None = None,
    paths: Sequence[str] | None = None,
    *,
    policy: Policy,
) -> dict[str, FileScan]:
    selected = (
        (*universe.python_files, *typescript_files(universe, policy)) if paths is None else paths
    )
    if not selected:
        return {}
    ts_paths = [p for p in selected if not p.endswith(".py")]
    py_paths = [p for p in selected if p.endswith(".py")]
    scans = _scan_ts_cached(universe, ts_paths, sources, policy)
    scans.update(_scan_python_cached(universe, sources, py_paths, jobs, policy))
    return scans
