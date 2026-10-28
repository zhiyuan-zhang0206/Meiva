# meiva:note
# How far changes reach: the impact census numbers: how far changes reach through the selection graph.
#
# `impact_census.py` builds the impact graph from the checkout and prints; the numbers it reports —
# source files every test reaches, the inputs nothing bounds, and the tests that pull them in — are
# computed here from that graph and the timing model.

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Set
from dataclasses import dataclass

__all__ = ["Census", "Site", "census", "report_lines"]


@dataclass(frozen=True)
class Site:
    # meiva:note
    # One unbounded input and the tests that reach it.

    path: str
    line: int
    reason: str
    tests: int
    minutes: float


@dataclass(frozen=True)
class Census:
    # meiva:note
    # Reach and unbounded-input numbers for one checkout. `by_package` maps a package to its
    # file count and how many of those every test reaches.

    tests: int
    full_minutes: float
    source_files: int
    sources_reached_by_every_test: int
    by_package: dict[str, tuple[int, int]]
    unbounded_sites: int
    unbounded_sites_reached_by_every_test: int
    tests_reaching_unbounded: int
    tests_reaching_unbounded_minutes: float
    heaviest_unbounded: tuple[Site, ...]


def census(
    tests: Set[str],
    tests_by_input: Mapping[str, Set[str]],
    unbounded: tuple[tuple[str, int, str], ...],
    *,
    seconds_of: Callable[[frozenset[str]], float],
    top: int = 10,
) -> Census:
    # meiva:note
    # Measure the impact graph of one checkout: `seconds_of` estimates a selected subset.
    reference = frozenset(tests)

    def minutes(selected: Set[str]) -> float:
        return round(seconds_of(frozenset(selected)) / 60, 1)

    total = len(tests)
    packages, everywhere = _package_reach(tests_by_input, tests)
    by_site = _unbounded_sites(tests_by_input, unbounded)
    tainted: set[str] = set()
    for consumers in by_site.values():
        tainted |= consumers
    heaviest = sorted(by_site.items(), key=lambda entry: (-minutes(entry[1]), entry[0]))[:top]
    return Census(
        tests=total,
        full_minutes=minutes(reference),
        source_files=sum(packages.values()),
        sources_reached_by_every_test=sum(everywhere.values()),
        by_package={name: (count, everywhere[name]) for name, count in sorted(packages.items())},
        unbounded_sites=len(by_site),
        unbounded_sites_reached_by_every_test=sum(1 for t in by_site.values() if len(t) == total),
        tests_reaching_unbounded=len(tainted),
        tests_reaching_unbounded_minutes=minutes(tainted),
        heaviest_unbounded=tuple(
            Site(path, line, reason, len(t), minutes(t)) for (path, line, reason), t in heaviest
        ),
    )


def _package_reach(
    tests_by_input: Mapping[str, Set[str]], tests: Set[str]
) -> tuple[Counter[str], Counter[str]]:
    # meiva:note
    # Per top-level package: non-test source files, and those every test reaches.
    packages: Counter[str] = Counter()
    everywhere: Counter[str] = Counter()
    for path, consumers in tests_by_input.items():
        if not path.endswith(".py") or "/" not in path or path in tests:
            continue
        if "/tests/" in path or path.startswith("tests/"):
            continue
        package = path.split("/", 1)[0]
        packages[package] += 1
        if len(consumers) == len(tests):
            everywhere[package] += 1
    return packages, everywhere


def _unbounded_sites(
    tests_by_input: Mapping[str, Set[str]], unbounded: tuple[tuple[str, int, str], ...]
) -> dict[tuple[str, int, str], Set[str]]:
    # meiva:note
    # Each unbounded input with the tests that reach the file holding it.
    by_site: defaultdict[tuple[str, int, str], Set[str]] = defaultdict(frozenset)
    for path, line, reason in unbounded:
        by_site[(path, line, reason)] |= tests_by_input.get(path, frozenset())
    return dict(by_site)


def report_lines(report: Census) -> list[str]:
    # meiva:note
    # The census as the text the command prints, one fact per line.
    lines = [
        f"sources reached by every test: {report.sources_reached_by_every_test}"
        f" / {report.source_files}"
    ]
    for package, (files, everywhere) in report.by_package.items():
        lines.append(f"  {package}: {everywhere} / {files}")
    lines.append(
        f"unbounded sites: {report.unbounded_sites}"
        f" (reached by every test: {report.unbounded_sites_reached_by_every_test})"
    )
    lines.append(
        f"tests reaching an unbounded site: {report.tests_reaching_unbounded} / {report.tests}"
        f" ({report.tests_reaching_unbounded_minutes} of {report.full_minutes} estimated minutes)"
    )
    for site in report.heaviest_unbounded:
        lines.append(
            f"  {site.minutes:5.1f} min {site.tests:4d} tests  {site.path}:{site.line}: {site.reason}"
        )
    return lines
