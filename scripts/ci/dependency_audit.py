#!/usr/bin/env python

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from functools import partial
from http.client import HTTPResponse
from pathlib import Path
from typing import Literal, Protocol

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# meiva:note
# The weekly workflow runs this file as a script with the interpreter CI provides, so the checkout
# root comes first on sys.path for `base.host.boundary` (standard library only).
from base.host.boundary.fields import (  # noqa: E402
    optional_object_field,
    optional_str_field,
    str_field,
    typed_optional_list_field,
    typed_optional_object_field,
    typed_optional_str_field,
)
from base.host.boundary.json_values import is_object_dict, is_object_list  # noqa: E402
from base.host.boundary.options import options  # noqa: E402
from base.host.boundary.untyped import object_of  # noqa: E402

MARKER = "<!-- weekly-dependency-audit -->"
_TITLE = "Weekly dependency audit: findings need attention"
_UV_PIN = ("uv", "base/host/tool_pins.py", r'^UV_VERSION = "([^"]+)"')
_PNPM_PIN = ("pnpm", "base/host/tool_pins.py", r'^PNPM_VERSION = "([^"]+)"')
_SEVERITY_RANK = {"critical": 4, "high": 3, "moderate": 2, "low": 1, "unknown": 0}
_ACTIONABLE_SEVERITIES = frozenset({"critical", "high", "unknown"})

# meiva:note
# A JSON read by URL or API path, handed back as `object` for the caller to narrow.
Fetch = Callable[[str], object]
type UrlOpener = Callable[
    [str | urllib.request.Request, float], AbstractContextManager[HTTPResponse]
]


class AuditProcess(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        /,
        *,
        timeout: float,
        capture_output: Literal[True],
        text: Literal[True],
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True)
class Vulnerability:
    ecosystem: str
    package: str
    version: str
    advisory: str
    severity: str
    fix: str
    summary: str
    dev_only_no_fix: bool = False


@dataclass(frozen=True)
class Binary:
    name: str
    pinned: str
    latest: str
    source: str

    @property
    def behind(self) -> bool:
        return _version_key(self.latest) > _version_key(self.pinned)


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part[0]) for part in re.finditer(r"\d+", version.lstrip("v")))


@dataclass(frozen=True)
class Options:
    out: Path
    sync_issue: str | None


def _fetch_json(url: str, *, open_url: UrlOpener) -> object:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with open_url(request, 30) as response:
        return object_of(json.load, response)


def _fetch_text(url: str, *, open_url: UrlOpener) -> str:
    with open_url(url, 30) as response:
        return response.read().decode()


def _gh_api(path: str, *, run: AuditProcess) -> object:
    return object_of(json.loads, _gh(["api", path], run=run))


def _required_token(record: Mapping[str, object], key: str) -> str:
    value = str_field(record, key).strip()
    if not value:
        raise ValueError(f"{key} is empty")
    return value


_ATX_HEADING_RE = re.compile(r"^ {0,3}#{1,6}(?:\s+(.*))?$")


# meiva:note
# Collapses multi-line advisory prose and skips leading ATX Markdown headings (1-6 `#` followed by whitespace or end-of-line, such as `### Summary`) while preserving non-heading `#` prefixes like `#123` or `#######`.
def _prose_line(raw: str | None) -> str | None:
    if raw is None:
        return None
    heading_bodies: list[str] = []
    for raw_line in raw.splitlines():
        collapsed = " ".join(raw_line.split())
        if not collapsed:
            continue
        match = _ATX_HEADING_RE.match(raw_line)
        if match is None:
            return collapsed
        if body := " ".join((match.group(1) or "").split()):
            heading_bodies.append(body)
    return heading_bodies[0] if heading_bodies else None


def _osv_record_prose(record: Mapping[str, object]) -> tuple[str | None, str | None]:
    return (
        _prose_line(optional_str_field(record, "summary")),
        _prose_line(optional_str_field(record, "details")),
    )


def _osv_lookup(advisory_ids: Sequence[str], fetch: Fetch) -> tuple[str, str | None]:
    summary: str | None = None
    details: str | None = None
    for advisory_id in dict.fromkeys(advisory_ids):
        try:
            record: object = fetch(f"https://api.osv.dev/v1/vulns/{advisory_id}")
        except (OSError, ValueError):
            continue
        if not is_object_dict(record):
            continue
        rec_summary, rec_details = _osv_record_prose(record)
        summary = summary or rec_summary
        details = details or rec_details
        specific = optional_object_field(record, "database_specific") or {}
        severity = specific.get("severity")
        if isinstance(severity, str) and severity.strip().lower() in _SEVERITY_RANK:
            return severity.strip().lower(), summary or details
    return "unknown", summary or details


# meiva:note
# uv audit JSON carries no severity; each advisory id is graded through OSV's reviewed severity, and an advisory OSV cannot grade is unknown and counts as actionable.
# meiva:link scripts/ci/docs/dependency-audit.md
def osv_severity(advisory_ids: list[str], fetch: Fetch) -> str:
    severity, _ = _osv_lookup(advisory_ids, fetch)
    return severity


def _aliases(entry: Mapping[str, object]) -> list[str]:
    raw_aliases = typed_optional_list_field(entry, "aliases") or []
    ghsa: list[str] = []
    for item in raw_aliases:
        if item is None:
            continue
        if not isinstance(item, str):
            raise TypeError(f"aliases entry is {type(item).__name__}, not str")
        if (cleaned := item.strip()).startswith("GHSA-"):
            ghsa.append(cleaned)
    return ghsa


def _fix_versions(entry: Mapping[str, object]) -> str:
    raw_fixes = typed_optional_list_field(entry, "fix_versions") or []
    cleaned: list[str] = []
    for item in raw_fixes:
        if item is None:
            continue
        if not isinstance(item, str):
            raise TypeError(f"fix_versions entry is {type(item).__name__}, not str")
        if stripped := item.strip():
            cleaned.append(stripped)
    return ", ".join(cleaned) or "none"


# meiva:note
# OSV and PyPA advisories returned by `uv audit` allow `summary` to be null; fall back through `description`, `details`, OSV prose, `link`, and the advisory id while still rejecting non-string corruption.
# meiva:link scripts/ci/docs/dependency-audit.md
def _python_summary(entry: Mapping[str, object], osv_fallback: str | None, advisory: str) -> str:
    prose_fields = [
        _prose_line(typed_optional_str_field(entry, key))
        for key in ("summary", "description", "details")
    ]
    link = _prose_line(typed_optional_str_field(entry, "link"))
    for candidate in (*prose_fields, osv_fallback, link):
        if candidate:
            return candidate
    return advisory


def python_vulnerabilities(audit_json: Mapping[str, object], fetch: Fetch) -> list[Vulnerability]:
    entries = audit_json.get("vulnerabilities")
    if not is_object_list(entries):
        raise TypeError("uv audit JSON vulnerabilities is not a list")
    found: list[Vulnerability] = []
    osv_cache: dict[tuple[str, ...], tuple[str, str | None]] = {}
    for entry in entries:
        if not is_object_dict(entry):
            raise TypeError("uv audit JSON vulnerability is not an object")
        dependency = typed_optional_object_field(entry, "dependency")
        if dependency is None:
            raise TypeError("uv audit JSON vulnerability lacks its dependency")
        advisory_id = _required_token(entry, "id")
        advisory = _prose_line(typed_optional_str_field(entry, "display_id")) or advisory_id
        ids = tuple(dict.fromkeys([advisory_id, *_aliases(entry)]))
        if ids not in osv_cache:
            osv_cache[ids] = _osv_lookup(ids, fetch)
        severity, osv_fallback = osv_cache[ids]
        found.append(
            Vulnerability(
                ecosystem="python",
                package=_required_token(dependency, "name"),
                version=_required_token(dependency, "version"),
                advisory=advisory,
                severity=severity,
                fix=_fix_versions(entry),
                summary=_python_summary(entry, osv_fallback, advisory),
            )
        )
    return found


def _unquote(raw: str) -> str:
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def _lock_body_lines(lock_text: str) -> list[str]:
    lines = [
        line
        for line in lock_text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not lines or not lines[0].startswith("lockfileVersion:"):
        raise ValueError("invalid pnpm-lock.yaml header")
    if _unquote(lines[0].partition(":")[2]) != "9.0":
        raise ValueError("invalid pnpm-lock.yaml header")
    return lines[1:]


def _yaml_line_key(line: str) -> tuple[int, str] | None:
    indent = len(line) - len(line.lstrip(" "))
    key, sep, _ = line.strip().partition(":")
    if not sep or indent % 2:
        return None
    return indent // 2, _unquote(key)


def _record_root_dep(
    scope: tuple[str, ...], level: int, key: str, runtime_deps: set[str], dev_deps: set[str]
) -> None:
    if level != 3 or scope[:2] != ("importers", "."):
        return
    if scope[2] == "dependencies":
        runtime_deps.add(key)
    elif scope[2] == "devDependencies":
        dev_deps.add(key)


def pnpm_dev_only_roots(lock_text: str) -> frozenset[str]:
    scope: tuple[str, ...] = ("", "", "", "")
    saw_root = False
    runtime_deps: set[str] = set()
    dev_deps: set[str] = set()
    for line in _lock_body_lines(lock_text):
        parsed = _yaml_line_key(line)
        if parsed is None:
            continue
        level, clean_key = parsed
        if level == 0 and scope[0] == "importers":
            break
        if level < 4:
            scope = (*scope[:level], clean_key, *("",) * (3 - level))
            saw_root = saw_root or scope[:2] == ("importers", ".")
            _record_root_dep(scope, level, clean_key, runtime_deps, dev_deps)
    if not saw_root:
        raise ValueError("pnpm-lock.yaml missing root importer")
    return frozenset(dev_deps - runtime_deps)


def _finding_paths_dev_only(paths: object, dev_only_roots: frozenset[str] | None) -> bool:
    if dev_only_roots is None:
        return True
    if not is_object_list(paths) or not paths:
        return False
    for path in paths:
        if not isinstance(path, str) or not path.startswith(".>"):
            return False
        direct = path[2:].partition(">")[0]
        if direct not in dev_only_roots:
            return False
    return True


def _findings_dev_only(findings: object, dev_only_roots: frozenset[str] | None) -> bool:
    if not is_object_list(findings) or not findings:
        return False
    return all(
        is_object_dict(finding)
        and finding.get("dev") is True
        and _finding_paths_dev_only(finding.get("paths"), dev_only_roots)
        for finding in findings
    )


def _no_forward_fix(patched: str, versions: list[str]) -> bool:
    cleaned = patched.strip()
    if not cleaned or cleaned == "<0.0.0":
        return True
    if versions and re.fullmatch(r">=?\s*v?[\d.]+", cleaned):
        target = _version_key(cleaned)
        return all(target <= _version_key(version) for version in versions)
    return False


def _advisory_versions(findings: list[object]) -> list[str]:
    versions: set[str] = set()
    for finding in findings:
        if not is_object_dict(finding):
            raise TypeError("pnpm advisory finding is not an object")
        if version := _prose_line(typed_optional_str_field(finding, "version")):
            versions.add(version)
    return sorted(versions)


def _pnpm_advisory_id(key: str, entry: Mapping[str, object]) -> str:
    ghsa = _prose_line(typed_optional_str_field(entry, "github_advisory_id"))
    raw_id = entry.get("id")
    if raw_id is None:
        fallback_id: str | None = None
    elif isinstance(raw_id, int) and not isinstance(raw_id, bool):
        fallback_id = str(raw_id)
    elif isinstance(raw_id, str):
        fallback_id = _prose_line(raw_id)
    else:
        raise TypeError(f"id is {type(raw_id).__name__}, not int | str")
    return ghsa or fallback_id or key


def _pnpm_summary(entry: Mapping[str, object], advisory: str) -> str:
    candidates = [
        _prose_line(typed_optional_str_field(entry, key)) for key in ("title", "overview", "url")
    ]
    for candidate in candidates:
        if candidate:
            return candidate
    return advisory


def _advisory_vulnerability(
    key: str, entry: Mapping[str, object], dev_only_roots: frozenset[str] | None
) -> Vulnerability:
    findings = typed_optional_list_field(entry, "findings") or []
    versions = _advisory_versions(findings)
    patched = (typed_optional_str_field(entry, "patched_versions") or "").strip()
    raw_severity = (typed_optional_str_field(entry, "severity") or "").strip().lower()
    severity = raw_severity if raw_severity in _SEVERITY_RANK else "unknown"
    advisory = _pnpm_advisory_id(key, entry)
    exempt = _findings_dev_only(findings, dev_only_roots) and _no_forward_fix(patched, versions)
    fallback_version = (
        _prose_line(typed_optional_str_field(entry, "vulnerable_versions")) or "unknown"
    )
    return Vulnerability(
        ecosystem="pnpm",
        package=_required_token(entry, "module_name"),
        version=", ".join(versions) or fallback_version,
        advisory=advisory,
        severity=severity,
        fix="none" if (not patched or patched == "<0.0.0") else patched,
        summary=_pnpm_summary(entry, advisory),
        dev_only_no_fix=exempt,
    )


# meiva:link scripts/ci/docs/dependency-audit.md
def pnpm_vulnerabilities(
    audit_json: Mapping[str, object], dev_only_roots: frozenset[str] | None = None
) -> list[Vulnerability]:
    raw_advisories: object = audit_json.get("advisories")
    if not is_object_dict(raw_advisories):
        raise TypeError("pnpm audit JSON missing advisories map")
    found: list[Vulnerability] = []
    for key in sorted(raw_advisories):
        entry: object = raw_advisories[key]
        if not is_object_dict(entry):
            raise TypeError(f"pnpm advisory {key!r} is not an object")
        found.append(_advisory_vulnerability(key, entry, dev_only_roots))
    return found


# meiva:note
# An audit tool exiting 1 with complete JSON findings is accepted; non-JSON output, an error payload or any other exit code raises as an incomplete audit.
# meiva:link scripts/ci/docs/dependency-audit.md
def run_json(argv: list[str], cwd: Path, *, run: AuditProcess) -> dict[str, object]:
    result = run(argv, capture_output=True, text=True, cwd=cwd, timeout=600)
    try:
        parsed = object_of(json.loads, result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{argv[0]} produced no JSON (rc={result.returncode}): {result.stderr[-400:]}"
        ) from exc
    if not is_object_dict(parsed):
        raise TypeError(f"{argv[0]} JSON is not an object")
    if "error" in parsed or result.returncode not in (0, 1):
        raise RuntimeError(f"{argv[0]} reported an audit error (rc={result.returncode})")
    return parsed


# meiva:link scripts/ci/docs/dependency-audit.md
PINS: tuple[tuple[str, str, str], ...] = (
    ("zonky Postgres", "base/cluster/dataplane/runtime_binaries.py", r'^PG_VERSION = "([^"]+)"'),
    ("pgvector", "base/cluster/dataplane/runtime_binaries.py", r'^PGVECTOR_VERSION = "([^"]+)"'),
    (
        "otelcol-contrib",
        "base/deploy/release/collector/plan.py",
        r'^OTELCOL_CONTRIB_VERSION = "([^"]+)"',
    ),
    _UV_PIN,
    _PNPM_PIN,
    ("Grafana", "deploy/lgtm/native/versions.yml", r"^grafana:\n  version: (\S+)$"),
)


def pinned_version(path: str, pattern: str, root: Path = _REPO_ROOT) -> str:
    text = (root / path).read_text(encoding="utf-8")
    matches = [match[1] for match in re.finditer(pattern, text, re.MULTILINE)]
    if len(matches) != 1:
        raise RuntimeError(f"{path}: expected exactly one match for {pattern!r}, found {matches}")
    return matches[0]


# meiva:note
# The zonky Postgres row compares only within the pinned major: a major bump is an expand step, not what the row reports.
# meiva:link scripts/ci/docs/dependency-audit.md
def zonky_latest(pinned: str, metadata_xml: str) -> str:
    major = pinned.split(".", 1)[0]
    versions = [match[1] for match in re.finditer(r"<version>([^<]+)</version>", metadata_xml)]
    same_major = [v for v in versions if v.split(".", 1)[0] == major and re.fullmatch(r"[\d.]+", v)]
    return max(same_major, key=_version_key)


def latest_binaries(
    gh_api: Fetch, fetch_text: Callable[[str], str], root: Path = _REPO_ROOT
) -> list[Binary]:
    pins = {name: pinned_version(path, pattern, root) for name, path, pattern in PINS}

    def release(repo: str) -> str:
        data = gh_api(f"repos/{repo}/releases/latest")
        if not is_object_dict(data):
            raise TypeError(f"{repo} latest release is not an object")
        return _required_token(data, "tag_name")

    tags = gh_api("repos/pgvector/pgvector/tags?per_page=100")
    if not is_object_list(tags):
        raise TypeError("pgvector tags are not a list")
    pgvector_tags = [
        name
        for tag in tags
        if is_object_dict(tag) and re.fullmatch(r"v\d+\.\d+\.\d+", name := str_field(tag, "name"))
    ]
    maven = "https://repo1.maven.org/maven2/io/zonky/test/postgres/embedded-postgres-binaries-linux-amd64"
    return [
        Binary(
            "zonky Postgres",
            pins["zonky Postgres"],
            zonky_latest(pins["zonky Postgres"], fetch_text(f"{maven}/maven-metadata.xml")),
            "maven central (same major)",
        ),
        Binary(
            "pgvector",
            pins["pgvector"],
            max(pgvector_tags, key=_version_key),
            "pgvector/pgvector tags",
        ),
        Binary(
            "otelcol-contrib",
            pins["otelcol-contrib"],
            release("open-telemetry/opentelemetry-collector-releases"),
            "opentelemetry-collector-releases",
        ),
        Binary("uv", pins["uv"], release("astral-sh/uv"), "astral-sh/uv releases"),
        Binary("pnpm", pins["pnpm"], release("pnpm/pnpm"), "pnpm/pnpm releases"),
        Binary("Grafana", pins["Grafana"], release("grafana/grafana"), "grafana/grafana releases"),
    ]


# meiva:note
# A run is actionable when any finding is critical, high or unknown, or any pinned binary is behind; a dev-only finding with no forward fix is listed but does not make the run actionable.
# meiva:link scripts/ci/docs/dependency-audit.md
def is_actionable(vulnerabilities: list[Vulnerability], binaries: list[Binary]) -> bool:
    return any(
        v.severity in _ACTIONABLE_SEVERITIES and not v.dev_only_no_fix for v in vulnerabilities
    ) or any(b.behind for b in binaries)


def render(vulnerabilities: list[Vulnerability], binaries: list[Binary]) -> str:
    lines = [
        MARKER,
        "",
        "Weekly dependency audit (`scripts/ci/dependency_audit.py`). No pin was changed.",
        "",
    ]
    for ecosystem, heading in (
        ("python", "Python (uv audit + OSV severity)"),
        ("pnpm", "Frontend (pnpm audit)"),
    ):
        lines += _ecosystem_section(ecosystem, heading, vulnerabilities)
    lines += [
        "## Pinned binaries",
        "",
        "| Binary | Pinned | Latest | Source | Status |",
        "|---|---|---|---|---|",
    ]
    lines += [
        f"| {b.name} | {b.pinned} | {b.latest} | {b.source} | {'behind' if b.behind else 'current'} |"
        for b in binaries
    ]
    lines.append("")
    return "\n".join(lines)


def _ecosystem_section(
    ecosystem: str, heading: str, vulnerabilities: list[Vulnerability]
) -> list[str]:
    rows = sorted(
        (v for v in vulnerabilities if v.ecosystem == ecosystem),
        key=lambda v: -_SEVERITY_RANK[v.severity],
    )
    counts = {s: sum(1 for v in rows if v.severity == s) for s in _SEVERITY_RANK}
    summary = ", ".join(f"{n} {s}" for s, n in counts.items() if n) or "none"
    lines = [f"## {heading}", "", f"Findings: {summary}.", ""]
    if rows:
        lines += [
            "| Severity | Package | Version | Advisory | Fix | Summary |",
            "|---|---|---|---|---|---|",
        ]
        lines += [
            f"| {v.severity} | {_cell(v.package)} | {_cell(v.version)} | {_cell(v.advisory)} | {_cell(v.fix)} | {_cell(v.summary)} |"
            for v in rows
        ]
        lines.append("")
    exempt = sum(1 for v in rows if v.dev_only_no_fix)
    if exempt:
        lines += [
            f"{exempt} finding(s) are dev-only with no forward fix and are not actionable.",
            "",
        ]
    return lines


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ")[:200]


def _gh(argv: list[str], *, run: AuditProcess) -> str:
    result = run(["gh", *argv], capture_output=True, text=True, timeout=60)
    result.check_returncode()
    return result.stdout


# meiva:note
# `urlopen` types its answer `Any`; a parameter typed with the real `HTTPResponse` takes it without naming the `Any`.
def _opened(
    opener: Callable[[str | urllib.request.Request], AbstractContextManager[HTTPResponse]],
    target: str | urllib.request.Request,
) -> AbstractContextManager[HTTPResponse]:
    return opener(target)


def _system_open(
    target: str | urllib.request.Request, timeout: float
) -> AbstractContextManager[HTTPResponse]:
    return _opened(partial(urllib.request.urlopen, timeout=timeout), target)


# meiva:note
# The one tracked issue is located by its marker among non-pull-request issues, updated while the run is actionable, opened when missing, and closed with a comment when clean.
# meiva:link scripts/ci/docs/dependency-audit.md
def sync_issue(repo: str, body: str, *, actionable: bool, gh: Callable[[list[str]], str]) -> str:
    marked = f'select((.body // "") | contains("{MARKER}"))'
    listing = gh(
        [
            "api",
            "--method",
            "GET",
            f"repos/{repo}/issues",
            "-f",
            "state=open",
            "-f",
            "per_page=100",
            "--paginate",
            "--jq",
            f'.[] | select(has("pull_request") | not) | {marked} | .number',
        ]
    )
    open_issues = listing.split()
    if actionable:
        if open_issues:
            gh(
                [
                    "api",
                    "--method",
                    "PATCH",
                    f"repos/{repo}/issues/{open_issues[0]}",
                    "-f",
                    f"body={body}",
                ]
            )
            return f"updated #{open_issues[0]}"
        created = gh(["issue", "create", "--repo", repo, "--title", _TITLE, "--body", body])
        return f"opened {created.strip()}"
    for number in open_issues:
        gh(
            [
                "api",
                "--method",
                "POST",
                f"repos/{repo}/issues/{number}/comments",
                "-f",
                "body=The weekly audit came back clean: no actionable findings.",
            ]
        )
        gh(
            [
                "api",
                "--method",
                "PATCH",
                f"repos/{repo}/issues/{number}",
                "-f",
                "state=closed",
                "-f",
                "state_reason=completed",
            ]
        )
    return f"closed {len(open_issues)} issue(s)"


# meiva:note
# A run renders the marker-prefixed three-section report from the audit tools and the canonical pins, and runs `uv audit` with the uv version pinned in base/host/tool_pins.py.
# meiva:link scripts/ci/docs/dependency-audit.md
def main(argv: list[str], *, run: AuditProcess, open_url: UrlOpener) -> int:
    parser = argparse.ArgumentParser(description="Weekly dependency audit report")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--sync-issue", metavar="OWNER/REPO", help="update the tracked issue in this repo"
    )
    args = options(parser.parse_args(argv), Options)

    uv_json = run_json(
        [
            "uvx",
            "--from",
            f"uv=={pinned_version(*_UV_PIN[1:])}",
            "uv",
            "audit",
            "--frozen",
            "--output-format",
            "json",
        ],
        _REPO_ROOT,
        run=run,
    )
    pnpm_json = run_json(
        ["pnpm", "audit", "--json", "--registry=https://registry.npmjs.org"],
        _REPO_ROOT / "ui/web",
        run=run,
    )
    dev_roots = pnpm_dev_only_roots(
        (_REPO_ROOT / "ui/web" / "pnpm-lock.yaml").read_text(encoding="utf-8")
    )
    vulnerabilities = [
        *python_vulnerabilities(uv_json, partial(_fetch_json, open_url=open_url)),
        *pnpm_vulnerabilities(pnpm_json, dev_roots),
    ]
    binaries = latest_binaries(partial(_gh_api, run=run), partial(_fetch_text, open_url=open_url))

    report = render(vulnerabilities, binaries)
    args.out.write_text(report, encoding="utf-8")
    actionable = is_actionable(vulnerabilities, binaries)
    print(
        f"actionable={actionable} vulnerabilities={len(vulnerabilities)} behind={sum(b.behind for b in binaries)}"
    )
    if args.sync_issue:
        print(sync_issue(args.sync_issue, report, actionable=actionable, gh=partial(_gh, run=run)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], run=subprocess.run, open_url=_system_open))
