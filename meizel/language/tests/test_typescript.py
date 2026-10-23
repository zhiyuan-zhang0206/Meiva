from __future__ import annotations

from pathlib import Path

from meizel.allowances import parse
from meizel.graph.model import Code, FactKind
from meizel.graph.universe import Universe
from meizel.language.typescript import scan_typescript, typescript_files


def _make_universe(files: list[str]) -> Universe:
    return Universe(Path("/repo"), files)


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

export function wideFn(a: number, b: number, c: number, d: number, e: number, f: number) {
  const [val] = useState(0);
  return fetch("/api") && Date.now() + val + a + b + c + d + e + f;
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
        "\n".join(f"export const v{i} = {i};" for i in range(8)) + "\nexport type T = number;\n"
    )
    mod_scan = scan_typescript("ui/web/src/lib/mod.ts", many_exports, universe, policy)
    assert any(item.code is Code.WIDE_PUBLIC_SURFACE for item in mod_scan.purity)
    door_scan = scan_typescript("ui/web/src/lib/index.ts", many_exports, universe, policy)
    assert not any(item.code is Code.WIDE_PUBLIC_SURFACE for item in door_scan.purity)
    assert typescript_files(universe) == ()
