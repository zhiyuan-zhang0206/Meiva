from __future__ import annotations

from itertools import product
from pathlib import Path

import pytest

from meizel.graph.universe import GitRepo, Universe
from meizel.language.typescript import scan_typescript

BUNDLED = "ui/web/src/lib/x.ts"


def acquired(source: str, path: str = BUNDLED) -> list[tuple[str, str]]:
    scan = scan_typescript(path, source, Universe(GitRepo(Path("/repo")), [path]))
    return [(item.kind.value, item.target) for item in scan.ambient]


@pytest.mark.parametrize(
    "source",
    [
        "export const base = process.env.NEXT_PUBLIC_API_BASE;\n",
        'export const base = process.env["NEXT_PUBLIC_API_BASE"];\n',
        "export const base = process.env.NEXT_PUBLIC_API_BASE?.trim();\n",
        "export const base = process.env?.NEXT_PUBLIC_API_BASE;\n",
        "export const base = (process.env).NEXT_PUBLIC_API_BASE;\n",
        "export const base = (process.env as Record<string, string>).NEXT_PUBLIC_API_BASE;\n",
        "export const base = process.env!.NEXT_PUBLIC_API_BASE;\n",
        "export const port = typeof process.env.NEXT_PUBLIC_PORT === 'string';\n",
        "export const url = `${process.env.NEXT_PUBLIC_A}/${process.env.NEXT_PUBLIC_B}`;\n",
        "let seen: string | undefined;\nseen = process.env.NEXT_PUBLIC_API_BASE;\nexport { seen };\n",
    ],
)
def test_a_next_public_variable_in_bundled_source_is_a_constant_of_the_build(source: str) -> None:
    assert acquired(source) == []


@pytest.mark.parametrize(
    "path",
    ["ui/web/next.config.ts", "ui/web/scripts/probe.ts", "ui/web/playwright.config.ts"],
)
def test_the_same_read_in_a_file_the_build_does_not_bundle_is_an_environment_read(
    path: str,
) -> None:
    source = "export const base = process.env.NEXT_PUBLIC_API_BASE;\n"
    assert acquired(source, path) == [("environ", "process.env")]


# meiva:note
# Turbopack, the default build, does not replace the optional-chain and imported-`process` spellings, and a write
# is not a read; the rest are the ways to the environment that were never a literal key.
COUNTED = [
    "export const mode = process.env.NODE_ENV;\n",
    "export const analyze = process.env.ANALYZE;\n",
    "export const named = process.env.PUBLIC_NEXT_API;\n",
    "export function read(name: string) { return process.env[name]; }\n",
    "export function read(name: string) { return process.env[name]?.trim(); }\n",
    "export function read(name: string) { return process.env[`NEXT_PUBLIC_${name}`]; }\n",
    "export const { NEXT_PUBLIC_API_BASE } = process.env;\n",
    "const env = process.env;\nexport const base = env.NEXT_PUBLIC_API_BASE;\n",
    "export const all = { ...process.env };\n",
    "export const base = process.env.NEXT_PUBLIC_;\n",
    "export const base = process?.env?.NEXT_PUBLIC_API_BASE;\n",
    "export const base = process?.env.NEXT_PUBLIC_API_BASE;\n",
    'export const base = process?.env?.["NEXT_PUBLIC_API_BASE"];\n',
    'import process from "node:process";\nexport const base = process.env.NEXT_PUBLIC_API_BASE;\n',
    'import { env } from "node:process";\nexport const base = env.NEXT_PUBLIC_API_BASE;\n',
    'process.env.NEXT_PUBLIC_API_BASE = "http://x";\n',
    'process.env["NEXT_PUBLIC_API_BASE"] = "http://x";\n',
    'process.env.NEXT_PUBLIC_API_BASE += "/";\n',
    'process.env.NEXT_PUBLIC_API_BASE ??= "http://x";\n',
    'process.env.NEXT_PUBLIC_API_BASE ||= "http://x";\n',
    'process.env.NEXT_PUBLIC_API_BASE &&= "http://x";\n',
    "delete process.env.NEXT_PUBLIC_API_BASE;\n",
    "process.env.NEXT_PUBLIC_COUNT++;\n",
    "--process.env.NEXT_PUBLIC_COUNT;\n",
    '(process.env.NEXT_PUBLIC_API_BASE) = "http://x";\n',
    '(process.env.NEXT_PUBLIC_API_BASE as string) = "http://x";\n',
]


@pytest.mark.parametrize("source", COUNTED)
def test_every_other_way_to_the_environment_still_counts(source: str) -> None:
    found = acquired(source)
    assert found != []
    assert {kind for kind, _ in found} == {"environ"}


def test_a_test_file_acquires_nothing_with_or_without_the_exclusion() -> None:
    source = "export const a = process.env.NEXT_PUBLIC_A;\nexport const b = process.env.NODE_ENV;\n"
    assert acquired(source, "ui/web/src/lib/x.test.ts") == []


BROWSER = {
    "location": "environ",
    "localStorage": "environ",
    "sessionStorage": "environ",
    "navigator": "platform",
    "matchMedia": "platform",
    "document.location": "environ",
    "document.cookie": "environ",
}


@pytest.mark.parametrize(
    ("base", "name"), list(product(["", "window.", "globalThis.", "self."], BROWSER))
)
def test_the_page_and_the_browser_it_runs_in_are_read_from_the_environment(
    base: str, name: str
) -> None:
    source = f"export const value = {base}{name}.member;\n"
    assert acquired(source) == [(BROWSER[name], f"{base}{name}")]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("export const saved = localStorage.getItem('a');\n", [("environ", "localStorage")]),
        (
            "export function pick(k: string) { return localStorage[k]; }\n",
            [("environ", "localStorage")],
        ),
        (
            "export const dark = window.matchMedia('(prefers-color-scheme: dark)');\n",
            [("platform", "window.matchMedia")],
        ),
        ("export const store = localStorage;\n", [("environ", "localStorage")]),
        (
            "export function f(g: (x: unknown) => void) { g(location); }\n",
            [("environ", "location")],
        ),
        ("export const nav = navigator;\n", [("platform", "navigator")]),
        (
            "export const { localStorage: store } = globalThis;\n",
            [("environ", "localStorage")],
        ),
        ("export const media = matchMedia;\n", [("platform", "matchMedia")]),
    ],
)
def test_a_browser_global_is_read_called_indexed_or_passed_as_a_value(
    source: str, expected: list[tuple[str, str]]
) -> None:
    assert acquired(source) == expected


@pytest.mark.parametrize(
    "source",
    [
        "export function f(location: { href: string }) { return location.href; }\n",
        "export const o = { location: 'x' };\nexport const l = o.location;\n",
        "export function f(page: { location: { href: string } }) { return page.location.href; }\n",
        "export function f(localStorage: Map<string, string>) { return localStorage.get('a'); }\n",
        "export function f(navigator: { userAgent: string }) { return navigator.userAgent; }\n",
        "export class C { localStorage = new Map<string, string>(); get() { return this.localStorage; } }\n",
        "export const where = 'window.location.href';\n",
        "export type Where = typeof window.location;\n",
        "export type Nav = typeof navigator;\n",
        "export const has = typeof localStorage !== 'undefined';\n",
        "export const has = typeof window !== 'undefined';\n",
        'import navigator from "navigator";\nexport const agent = navigator.userAgent;\n',
        'import { location } from "./router";\nexport const here = location.href;\n',
    ],
)
def test_a_local_name_a_property_a_feature_test_or_a_package_reads_nothing(source: str) -> None:
    assert acquired(source) == []


@pytest.mark.parametrize(
    "source",
    [
        "export const { location: loc } = window;\n",
        "export const top = parent.location;\n",
    ],
)
def test_the_reads_the_rule_does_not_see_are_pinned(source: str) -> None:
    assert acquired(source) == []
