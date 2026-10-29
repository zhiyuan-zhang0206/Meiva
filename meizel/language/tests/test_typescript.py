from __future__ import annotations

from pathlib import Path

from meizel.allowances import parse
from meizel.checkout import GitRepo
from meizel.graph.model import Code, FactKind
from meizel.graph.universe import Universe
from meizel.language.typescript import scan_typescript, typescript_files


def _make_universe(files: list[str]) -> Universe:
    return Universe(GitRepo(Path("/repo")), files)


def test_typescript_imports_and_aliases() -> None:
    policy, problems = parse(
        """
[typescript]
scopes = ["ui/web/src/lib"]
import-sources = ["ui/web/src/components"]
aliases = ["@/=ui/web/src/"]
"""
    )
    assert problems == []
    universe = _make_universe(
        [
            "ui/web/src/lib/contracts/types.ts",
            "ui/web/src/lib/fold/index.ts",
            "ui/web/src/lib/fold/timeline.ts",
            "ui/web/src/components/view.tsx",
        ]
    )
    source = """
// import { fake } from "@/lib/contracts/types";
import type { AgentRow } from "../contracts/types";
import { applyEvent } from "@/lib/fold";
import { useState } from "react";
const url = "https://example.com//not-a-comment";
export { applySystemEvent } from "./timeline";
"""
    scan = scan_typescript("ui/web/src/lib/fold/owner.ts", source, universe, policy)
    targets = [(f.dst, f.kind, f.names) for f in scan.facts]
    assert ("ui/web/src/lib/contracts/types.ts", FactKind.IMPORT, ("AgentRow",)) in targets
    assert ("ui/web/src/lib/fold/index.ts", FactKind.IMPORT, ("applyEvent",)) in targets
    assert ("ui/web/src/lib/fold/timeline.ts", FactKind.IMPORT, ("applySystemEvent",)) in targets
    assert scan.external == frozenset({"react"})

    comp_source = 'import { applyEvent } from "@/lib/fold";\nconst [x, setX] = useState(0);\n'
    comp_scan = scan_typescript("ui/web/src/components/view.tsx", comp_source, universe, policy)
    assert [f.dst for f in comp_scan.facts] == ["ui/web/src/lib/fold/index.ts"]
    assert comp_scan.purity == ()


def test_typescript_purity() -> None:
    policy, _ = parse('[typescript]\nscopes = ["ui/web/src/lib"]\n')
    universe = _make_universe(["ui/web/src/lib/state/settings.ts"])
    prod_source = """
let cached = 0;
const seen = new Map<string, number>();

export function passthrough(a: number, b: string) {
  return api.call(a, b);
}

export function wideFn(a: number, b: number, c: number, d: number, e: number, f: number, g: number) {
  const [val] = useState(0);
  return fetch("/api") && Date.now() + val + a + b + c + d + e + f + g;
}
"""
    prod_scan = scan_typescript("ui/web/src/lib/state/settings.ts", prod_source, universe, policy)
    codes = {(item.code, item.key) for item in prod_scan.purity}
    assert (Code.UNFROZEN_VALUE, "cached") in codes
    assert (Code.UNFROZEN_VALUE, "seen") in codes
    assert (Code.PASSTHROUGH_FUNCTION, "passthrough") in codes
    assert (Code.WIDE_PARAMS, "wideFn") in codes
    assert (Code.IMPURE_CALL, "wideFn#useState") in codes
    assert (Code.IMPURE_CALL, "wideFn#fetch") in codes
    assert (Code.IMPURE_CALL, "wideFn#Date.now") in codes


def test_typescript_patches() -> None:
    policy, _ = parse(
        """
[typescript]
scopes = ["ui/web/src/lib"]
aliases = ["@/=ui/web/src/"]
"""
    )
    universe = _make_universe(
        [
            "ui/web/src/lib/transport/api.ts",
            "ui/web/src/lib/state/settings.test.ts",
        ]
    )
    test_source = """
import { api } from "@/lib/transport/api";
vi.mock("@/lib/transport/api", () => ({ api: {} }));
vi.mock("react", () => ({}));
vi.spyOn(api, "getSettings");
vi.stubGlobal("fetch", vi.fn());
vi.stubEnv("NODE_ENV", "test");
"""
    test_scan = scan_typescript(
        "ui/web/src/lib/state/settings.test.ts", test_source, universe, policy
    )
    patch_set = {(p.form, p.target, p.code) for p in test_scan.patches}
    assert ("vi.mock", "ui/web/src/lib/transport/api.ts", Code.FIRST_PARTY_PATCH) in patch_set
    assert ("vi.mock", "react", Code.THIRD_PARTY_PATCH) in patch_set
    assert (
        "vi.spyOn",
        "ui/web/src/lib/transport/api.ts.getSettings",
        Code.FIRST_PARTY_PATCH,
    ) in patch_set
    assert ("vi.stubGlobal", "fetch", Code.STDLIB_PATCH) in patch_set
    assert ("vi.stubEnv", "NODE_ENV", Code.ENV_PATCH) in patch_set


def test_typescript_wide_public_surface_and_door_exemption() -> None:
    policy, _ = parse('[typescript]\nscopes = ["ui/web/src/lib"]\n')
    universe = _make_universe(["ui/web/src/lib/mod.ts", "ui/web/src/lib/index.ts"])
    many_exports = (
        "\n".join(f"export const v{i} = (): number => {i};" for i in range(8))
        + "\nexport type T = number;\n"
    )
    mod_scan = scan_typescript("ui/web/src/lib/mod.ts", many_exports, universe, policy)
    assert any(item.code is Code.WIDE_PUBLIC_SURFACE for item in mod_scan.purity)
    door_scan = scan_typescript("ui/web/src/lib/index.ts", many_exports, universe, policy)
    assert not any(item.code is Code.WIDE_PUBLIC_SURFACE for item in door_scan.purity)
    assert set(typescript_files(universe, policy)) == set(universe.files)


def test_typescript_import_clauses_do_not_consume_other_declarations() -> None:
    policy, _ = parse('[typescript]\nscopes = ["src"]\n')
    universe = _make_universe(["src/a.ts", "src/b.ts", "src/c.ts", "src/d.ts"])
    source = """
export class Registry { value = 1 }
export { value } from './a.ts'
import './b.ts'
import type { Shape } from './c.ts'
const loaded = import('./d.ts', { with: { type: 'json' } })
"""
    scan = scan_typescript("src/owner.ts", source, universe, policy)
    assert {(f.dst, f.line) for f in scan.facts} == {
        ("src/a.ts", 3),
        ("src/b.ts", 4),
        ("src/c.ts", 5),
        ("src/d.ts", 6),
    }


def test_typescript_source_examples_are_not_imports_or_patches() -> None:
    policy, _ = parse('[typescript]\nscopes = ["src"]\n')
    universe = _make_universe(["src/real.ts", "src/example.ts"])
    source = """
const example = `
import { example } from './example.ts'
export * from './example.ts'
import './example.ts'
import('./example.ts')
vi.mock('./example.ts')
vi.spyOn(example, 'read')
vi.stubEnv('HOME', '/tmp')
vi.stubGlobal('fetch', fake)
`
const quoted = "import { example } from './example.ts'"
import { real } from './real.ts'
vi.mock('./real.ts')
"""
    scan = scan_typescript("src/owner.test.ts", source, universe, policy)
    assert {f.dst for f in scan.facts} == {"src/real.ts"}
    assert [(p.form, p.target) for p in scan.patches] == [("vi.mock", "src/real.ts")]


def test_typescript_aliases_resolve_exact_and_most_specific_patterns() -> None:
    policy, _ = parse("""
[typescript]
scopes = ["src"]
aliases = [
    "@pkg/*=src/general/*", "@pkg=src/index.ts", "@pkg/detail/*=src/detail/*",
    "@pkg/detail/exact=src/exact.ts", "@suffix/*.js=src/suffix/*.ts", "@/=src/",
]
""")
    universe = _make_universe(
        [
            "src/index.ts",
            "src/exact.ts",
            "src/detail/leaf.ts",
            "src/general/leaf.ts",
            "src/suffix/leaf.ts",
            "src/legacy.ts",
        ]
    )
    source = """
import '@pkg'
import '@pkg/leaf'
import '@pkg/detail/leaf'
import '@pkg/detail/exact'
import '@suffix/leaf.js'
import '@/legacy'
import '@pkg-extra'
"""
    scan = scan_typescript("src/owner.ts", source, universe, policy)
    assert {f.dst for f in scan.facts} == set(universe.files)
    assert scan.external == frozenset({"@pkg-extra"})


def test_typescript_js_specifiers_resolve_source_before_emitted_file() -> None:
    policy, _ = parse('[typescript]\nscopes = ["src"]\n')
    universe = _make_universe(["src/a.ts", "src/a.js", "src/b.tsx", "src/b.jsx"])
    scan = scan_typescript("src/owner.ts", "import './a.js'; import './b.jsx'", universe, policy)
    assert [f.dst for f in scan.facts] == ["src/a.ts", "src/b.tsx"]
