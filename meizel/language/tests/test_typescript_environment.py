from __future__ import annotations

from pathlib import Path

import pytest

from meizel.graph.universe import Universe
from meizel.language.typescript import scan_typescript

BUNDLED = "ui/web/src/lib/x.ts"


def acquired(source: str, path: str = BUNDLED) -> list[tuple[str, str]]:
    scan = scan_typescript(path, source, Universe(Path("/repo"), [path]))
    return [(item.kind.value, item.target) for item in scan.ambient]


@pytest.mark.parametrize(
    "source",
    [
        "export const base = process.env.NEXT_PUBLIC_API_BASE;\n",
        'export const base = process.env["NEXT_PUBLIC_API_BASE"];\n',
        "export const base = process.env.NEXT_PUBLIC_API_BASE?.trim();\n",
        "export const port = typeof process.env.NEXT_PUBLIC_PORT === 'string';\n",
        "export const url = `${process.env.NEXT_PUBLIC_A}/${process.env.NEXT_PUBLIC_B}`;\n",
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


@pytest.mark.parametrize(
    "source",
    [
        "export const mode = process.env.NODE_ENV;\n",
        "export const analyze = process.env.ANALYZE;\n",
        "export const zone = process.env.MEIVA_DEV_ORIGINS;\n",
        "export const named = process.env.PUBLIC_NEXT_API;\n",
        "export function read(name: string) { return process.env[name]; }\n",
        "export function read(name: string) { return process.env[name]?.trim(); }\n",
        "export function read(name: string) { return process.env[`NEXT_PUBLIC_${name}`]; }\n",
        "export const { NEXT_PUBLIC_API_BASE } = process.env;\n",
        "const env = process.env;\nexport const base = env.NEXT_PUBLIC_API_BASE;\n",
        "export const all = { ...process.env };\n",
        'process.env.NEXT_PUBLIC_API_BASE = "http://x";\n',
        'process.env["NEXT_PUBLIC_API_BASE"] = "http://x";\n',
        "delete process.env.NEXT_PUBLIC_API_BASE;\n",
        "export const base = process.env.NEXT_PUBLIC_;\n",
    ],
)
def test_every_other_way_to_the_environment_still_counts(source: str) -> None:
    assert [kind for kind, _ in acquired(source)] == ["environ"] * len(acquired(source)) != []


def test_a_test_file_acquires_nothing_with_or_without_the_exclusion() -> None:
    source = "export const a = process.env.NEXT_PUBLIC_A;\nexport const b = process.env.NODE_ENV;\n"
    assert acquired(source, "ui/web/src/lib/x.test.ts") == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("export const href = window.location.href;\n", [("environ", "window.location")]),
        ("export const href = location.href;\n", [("environ", "location")]),
        ("export const href = document.location.href;\n", [("environ", "document.location")]),
        ("export const href = globalThis.location.href;\n", [("environ", "globalThis.location")]),
        ("export const jar = document.cookie;\n", [("environ", "document.cookie")]),
        ("export const saved = localStorage.getItem('a');\n", [("environ", "localStorage")]),
        (
            "export function keep() { window.sessionStorage.setItem('a', 'b'); }\n",
            [("environ", "window.sessionStorage")],
        ),
        (
            "export function pick(k: string) { return localStorage[k]; }\n",
            [("environ", "localStorage")],
        ),
        ("export const agent = navigator.userAgent;\n", [("platform", "navigator")]),
        (
            "export const dark = window.matchMedia('(prefers-color-scheme: dark)');\n",
            [("platform", "window.matchMedia")],
        ),
        (
            "export const wide = matchMedia('(min-width: 1px)').matches;\n",
            [("platform", "matchMedia")],
        ),
    ],
)
def test_the_page_and_the_browser_it_runs_in_are_read_from_the_environment(
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
        "export const where = 'window.location.href';\n",
        "export type Where = typeof window.location;\n",
    ],
)
def test_a_local_name_or_a_property_called_like_a_browser_global_reads_nothing(source: str) -> None:
    assert acquired(source) == []
