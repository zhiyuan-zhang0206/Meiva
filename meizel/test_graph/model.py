from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TypeGuard

__all__ = [
    "FACT_DYNAMIC_FIXTURE",
    "FACT_DYNAMIC_IMPORT",
    "FACT_IMPORT_ONLY",
    "FACT_PYTESTER_NATIVE",
    "FACT_SYNTAX_ERROR",
    "FACT_UNANCHORED_READ",
    "CutoffContext",
    "FileTestFacts",
    "ModuleTable",
    "RawFileTestFacts",
    "TestCheckout",
    "TestLanes",
    "TestSelection",
    "decode_raw_facts",
    "encode_raw_facts",
]

FACT_DYNAMIC_FIXTURE = 1
FACT_DYNAMIC_IMPORT = 2
FACT_IMPORT_ONLY = 4
FACT_PYTESTER_NATIVE = 8
FACT_SYNTAX_ERROR = 16
FACT_UNANCHORED_READ = 32


@dataclass(frozen=True, slots=True)
class ModuleTable:
    files_by_module: dict[str, str]
    kinds_by_module: dict[str, str]

    @classmethod
    def build(cls, python_files: tuple[str, ...], top_packages: frozenset[str]) -> ModuleTable:
        files: dict[str, str] = {}
        kinds: dict[str, str] = {}
        for rel in python_files:
            top = rel.split("/", 1)[0]
            if top not in top_packages and rel != "conftest.py":
                continue
            parts = rel.split("/")
            for depth in range(1, len(parts)):
                pkg = ".".join(parts[:depth])
                kinds.setdefault(pkg, "namespace")
            if rel.endswith("/__init__.py"):
                dotted = ".".join(parts[:-1])
                files[dotted] = rel
                kinds[dotted] = "package"
            elif rel.endswith(".py"):
                stem = rel.removesuffix(".py")
                dotted = ".".join(stem.split("/"))
                files.setdefault(dotted, rel)
                kinds.setdefault(dotted, "module")
        return cls(files_by_module=files, kinds_by_module=kinds)

    def file(self, dotted: str) -> str | None:
        return self.files_by_module.get(dotted)

    def kind(self, dotted: str) -> str | None:
        return self.kinds_by_module.get(dotted)

    def resolve_prefix(self, dotted: str, top_packages: frozenset[str]) -> str | None:
        parts = dotted.split(".")
        if not parts or parts[0] not in top_packages:
            return None
        for end in range(len(parts), 0, -1):
            candidate = ".".join(parts[:end])
            if candidate in self.kinds_by_module:
                return candidate
        return None

    def module_files(self, dotted: str, top_packages: frozenset[str]) -> tuple[str, ...]:
        parts = dotted.split(".")
        if not parts or parts[0] not in top_packages:
            return ()
        out: list[str] = []
        for end in range(1, len(parts)):
            init_file = self.files_by_module.get(".".join(parts[:end]))
            if init_file is not None and init_file.endswith("/__init__.py"):
                out.append(init_file)
        leaf = self.files_by_module.get(dotted)
        if leaf is not None and leaf not in out:
            out.append(leaf)
        return tuple(out)


# meiva:note
# Layout-independent per-file AST facts keyed only by file path and source digest so SQLite cache
# entries stay valid across branch switches that add or remove sibling modules.
@dataclass(frozen=True, slots=True)
class RawFileTestFacts:
    path: str
    direct_imports: tuple[str, ...]
    from_imports: tuple[tuple[int, str, tuple[tuple[str, str], ...]], ...]
    dynamic_modules: tuple[str, ...]
    anchored_resources: tuple[str, ...]
    fixtures: tuple[tuple[str, bool, tuple[str, ...]], ...]
    test_requests: tuple[str, ...]
    markers: tuple[str, ...]
    plugins: tuple[str, ...]
    unknown_sites: tuple[tuple[int, str, str], ...]
    flags: int


@dataclass(frozen=True, slots=True)
class FileTestFacts:
    path: str
    imports: tuple[str, ...]
    resources: tuple[str, ...]
    reexports: tuple[tuple[str, str], ...]
    fixtures: tuple[tuple[str, bool, tuple[str, ...]], ...]
    test_requests: tuple[str, ...]
    markers: tuple[str, ...]
    plugins: tuple[str, ...]
    unknown_sites: tuple[tuple[int, str, str], ...]
    flags: int


@dataclass(frozen=True, slots=True)
class TestCheckout:
    python_files: tuple[str, ...]
    all_files: frozenset[str]
    top_packages: frozenset[str]
    test_dirs: frozenset[str]
    collectable: frozenset[str]
    conftests: frozenset[str]
    global_files: frozenset[str]
    path_scopes: tuple[tuple[str, str, tuple[str, ...]], ...]
    policy_loads: tuple[tuple[str, tuple[str, ...]], ...]
    policy_external: frozenset[str]
    manifests: dict[str, str]
    native_spawners: frozenset[str] = frozenset()


def _is_object(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def _is_array(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_str_list(value: object) -> TypeGuard[list[str]]:
    return _is_array(value) and all(isinstance(item, str) for item in value)


def encode_raw_facts(facts: RawFileTestFacts) -> str:
    return json.dumps(
        {
            "p": facts.path,
            "di": list(facts.direct_imports),
            "fi": [
                [lvl, mod, [list(pair) for pair in pairs]] for lvl, mod, pairs in facts.from_imports
            ],
            "dm": list(facts.dynamic_modules),
            "ar": list(facts.anchored_resources),
            "fx": [[name, auto, list(reqs)] for name, auto, reqs in facts.fixtures],
            "tr": list(facts.test_requests),
            "mk": list(facts.markers),
            "pl": list(facts.plugins),
            "uk": [[ln, rsn, expr] for ln, rsn, expr in facts.unknown_sites],
            "fl": facts.flags,
        },
        separators=(",", ":"),
    )


def _decode_from_imports(
    raw: list[object],
) -> tuple[tuple[int, str, tuple[tuple[str, str], ...]], ...]:
    out: list[tuple[int, str, tuple[tuple[str, str], ...]]] = []
    for item in raw:
        if (
            _is_array(item)
            and len(item) == 3
            and isinstance(item[0], int)
            and isinstance(item[1], str)
            and _is_array(item[2])
        ):
            pairs = tuple(
                (p[0], p[1])
                for p in item[2]
                if _is_array(p) and len(p) == 2 and isinstance(p[0], str) and isinstance(p[1], str)
            )
            out.append((item[0], item[1], pairs))
    return tuple(out)


def _decode_fixtures(raw: list[object]) -> tuple[tuple[str, bool, tuple[str, ...]], ...]:
    return tuple(
        (item[0], item[1], tuple(item[2]))
        for item in raw
        if _is_array(item)
        and len(item) == 3
        and isinstance(item[0], str)
        and isinstance(item[1], bool)
        and _is_str_list(item[2])
    )


def _decode_unknown_sites(raw: list[object]) -> tuple[tuple[int, str, str], ...]:
    return tuple(
        (item[0], item[1], item[2])
        for item in raw
        if _is_array(item)
        and len(item) == 3
        and isinstance(item[0], int)
        and isinstance(item[1], str)
        and isinstance(item[2], str)
    )


def decode_raw_facts(payload: str) -> RawFileTestFacts:
    obj: object = json.loads(payload)
    if not _is_object(obj):
        raise ValueError("invalid RawFileTestFacts payload")
    path = obj.get("p")
    di = obj.get("di")
    fi = obj.get("fi")
    dm = obj.get("dm")
    ar = obj.get("ar")
    fx = obj.get("fx")
    tr = obj.get("tr")
    mk = obj.get("mk")
    pl = obj.get("pl")
    uk = obj.get("uk")
    fl = obj.get("fl")
    if (
        not isinstance(path, str)
        or not _is_str_list(di)
        or not _is_array(fi)
        or not _is_str_list(dm)
        or not _is_str_list(ar)
        or not _is_array(fx)
        or not _is_str_list(tr)
        or not _is_str_list(mk)
        or not _is_str_list(pl)
        or not _is_array(uk)
        or not isinstance(fl, int)
    ):
        raise ValueError("invalid RawFileTestFacts fields")
    return RawFileTestFacts(
        path=path,
        direct_imports=tuple(di),
        from_imports=_decode_from_imports(fi),
        dynamic_modules=tuple(dm),
        anchored_resources=tuple(ar),
        fixtures=_decode_fixtures(fx),
        test_requests=tuple(tr),
        markers=tuple(mk),
        plugins=tuple(pl),
        unknown_sites=_decode_unknown_sites(uk),
        flags=fl,
    )


@dataclass(frozen=True, slots=True)
class TestLanes:
    static: frozenset[str]
    native: frozenset[str]
    reasons: tuple[tuple[str, str], ...]

    def encode(self) -> str:
        return json.dumps(
            {
                "static": sorted(self.static),
                "native": sorted(self.native),
                "reasons": [list(item) for item in self.reasons],
            },
            separators=(",", ":"),
        )

    @classmethod
    def decode(cls, raw: str) -> TestLanes:
        parsed: object = json.loads(raw)
        if not _is_object(parsed):
            raise ValueError("invalid cached TestLanes payload")
        static_raw = parsed.get("static")
        native_raw = parsed.get("native")
        reasons_raw = parsed.get("reasons")
        if (
            not _is_str_list(static_raw)
            or not _is_str_list(native_raw)
            or not _is_array(reasons_raw)
        ):
            raise ValueError("invalid cached TestLanes fields")
        reasons_out: list[tuple[str, str]] = []
        for pair in reasons_raw:
            if (
                _is_array(pair)
                and len(pair) == 2
                and isinstance(pair[0], str)
                and isinstance(pair[1], str)
            ):
                reasons_out.append((pair[0], pair[1]))
        return cls(
            static=frozenset(static_raw),
            native=frozenset(native_raw),
            reasons=tuple(reasons_out),
        )


@dataclass(frozen=True, slots=True)
class CutoffContext:
    base_sources: dict[str, str]
    head_sources: dict[str, str]
    base_manifests: dict[str, str]
    head_manifests: dict[str, str]
    base_test_dirs: frozenset[str]
    base_collectable: frozenset[str]


@dataclass(frozen=True, slots=True)
class TestSelection:
    decision: str
    reason: str
    tests: tuple[str, ...]
    diagnostics: tuple[str, ...]

    def as_json(self) -> str:
        return json.dumps(
            {
                "decision": self.decision,
                "reason": self.reason,
                "tests": list(self.tests),
                "diagnostics": list(self.diagnostics),
            },
            indent=2,
            sort_keys=True,
        )
