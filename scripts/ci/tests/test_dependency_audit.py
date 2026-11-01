from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

from base.host.brew_pin import UV_VERSION
from base.host.net.tests.fake_url_opener import Answer, FakeUrlOpener
from base.host.proc import run_bounded
from base.host.system.tests.fake_commands import FakeCommands, Reply
from scripts.ci import dependency_audit as audit


@pytest.mark.parametrize("exit_code", [0, 1])
def test_audit_accepts_complete_findings(tmp_path: Path, exit_code: int) -> None:
    commands = FakeCommands()
    commands.on(["pnpm", "audit"], Reply(returncode=exit_code, stdout='{"advisories": {}}'))
    assert audit.run_json(["pnpm", "audit"], tmp_path, run=commands) == {"advisories": {}}
    assert commands.calls[0].timeout == 600
    assert commands.calls[0].kwargs["cwd"] == tmp_path


@pytest.mark.parametrize(
    ("exit_code", "stdout"),
    [(1, '{"error": {"code": "ENOAUDIT"}}'), (2, '{"advisories": {}}')],
)
def test_audit_rejects_registry_and_tool_errors(
    tmp_path: Path, exit_code: int, stdout: str
) -> None:
    commands = FakeCommands()
    commands.on(
        ["pnpm", "audit"],
        Reply(returncode=exit_code, stdout=stdout, stderr="private registry details"),
    )
    with pytest.raises(RuntimeError, match="pnpm reported an audit error") as error:
        audit.run_json(["pnpm", "audit"], tmp_path, run=commands)
    assert "private registry details" not in str(error.value)


def _osv(severity: str | None):
    def fetch(url: str):
        if url.endswith("PYSEC-1"):
            raise urllib.error.URLError("no record")
        return {"database_specific": {"severity": severity} if severity else {}}

    return fetch


def test_severity_comes_from_the_first_id_osv_can_grade() -> None:
    assert audit.osv_severity(["PYSEC-1", "GHSA-a"], _osv("HIGH")) == "high"


def test_an_ungradable_advisory_is_unknown_not_dropped() -> None:
    assert audit.osv_severity(["GHSA-a"], _osv(None)) == "unknown"
    assert audit.osv_severity(["PYSEC-1"], _osv("HIGH")) == "unknown"


def test_python_findings_carry_osv_severity_and_fix_versions() -> None:
    uv_json = {
        "vulnerabilities": [
            {
                "dependency": {"name": "pyjwt", "version": "2.14.0"},
                "id": "PYSEC-1",
                "display_id": "PYSEC-1",
                "aliases": ["GHSA-a", "CVE-1"],
                "summary": "DoS",
                "fix_versions": ["2.15.0"],
            }
        ]
    }
    (found,) = audit.python_vulnerabilities(uv_json, _osv("CRITICAL"))
    assert (found.package, found.severity, found.fix) == ("pyjwt", "critical", "2.15.0")


def test_pnpm_findings_flatten_the_advisories_map() -> None:
    pnpm_json = {
        "advisories": {
            "1001": {
                "id": 1001,
                "module_name": "brace-expansion",
                "severity": "high",
                "github_advisory_id": "GHSA-brace",
                "title": "CPU denial of service",
                "vulnerable_versions": "<1.1.21",
                "patched_versions": ">=1.1.21",
                "findings": [
                    {"version": "1.1.11", "paths": [".>eslint>brace-expansion"], "dev": True}
                ],
            },
            "1002": {
                "id": 1002,
                "module_name": "next",
                "severity": "moderate",
                "github_advisory_id": "GHSA-next",
                "title": "Middleware bypass",
                "vulnerable_versions": "<16.4.1",
                "patched_versions": "<0.0.0",
                "findings": [{"version": "16.3.8", "paths": [".>next"], "dev": False}],
            },
        }
    }
    brace, nxt = audit.pnpm_vulnerabilities(pnpm_json)
    assert (brace.severity, brace.advisory, brace.fix) == ("high", "GHSA-brace", ">=1.1.21")
    assert (nxt.version, nxt.fix, nxt.summary) == ("16.3.8", "none", "Middleware bypass")


def _pnpm_findings(
    patched_versions: str,
    *,
    dev: bool = True,
    paths: list[str] | None = None,
) -> dict[str, object]:
    return {
        "advisories": {
            "2001": {
                "id": 2001,
                "module_name": "braces",
                "severity": "high",
                "github_advisory_id": "GHSA-braces",
                "title": "stack exhaustion",
                "vulnerable_versions": "<=3.0.3",
                "patched_versions": patched_versions,
                "findings": [
                    {
                        "version": "3.0.3",
                        "paths": paths if paths is not None else [".>shadcn>braces"],
                        "dev": dev,
                    }
                ],
            }
        }
    }


def _dev_roots() -> frozenset[str]:
    return audit.pnpm_dev_only_roots(
        "lockfileVersion: '9.0'\n\n"
        "importers:\n"
        "  .:\n"
        "    dependencies:\n"
        "      next:\n"
        "        specifier: 16.3.8\n"
        "        version: 16.3.8\n"
        "    devDependencies:\n"
        "      shadcn:\n"
        "        specifier: ^4.10.0\n"
        "        version: 4.21.1\n"
    )


@pytest.mark.parametrize("patched", ["<0.0.0", ">=1.0.0"], ids=["no-fix", "downgrade"])
def test_a_dev_only_finding_without_a_forward_fix_is_listed_but_not_actionable(
    patched: str,
) -> None:
    (found,) = audit.pnpm_vulnerabilities(_pnpm_findings(patched), _dev_roots())
    assert found.dev_only_no_fix
    assert not audit.is_actionable([found], [])
    report = audit.render([found], [])
    assert "| high | braces |" in report
    assert "not actionable" in report


def test_a_dev_only_suggestion_at_the_installed_version_is_not_a_forward_fix() -> None:
    (found,) = audit.pnpm_vulnerabilities(_pnpm_findings(">=3.0.3"), _dev_roots())
    assert found.dev_only_no_fix
    assert not audit.is_actionable([found], [])


def test_a_dev_only_finding_with_an_upgrade_stays_actionable() -> None:
    (found,) = audit.pnpm_vulnerabilities(_pnpm_findings(">=3.0.4"), _dev_roots())
    assert not found.dev_only_no_fix
    assert audit.is_actionable([found], [])


def test_a_production_finding_without_a_forward_fix_stays_actionable() -> None:
    (found,) = audit.pnpm_vulnerabilities(
        _pnpm_findings("<0.0.0", dev=False, paths=[".>next>braces"]), _dev_roots()
    )
    assert not found.dev_only_no_fix
    assert audit.is_actionable([found], [])


def test_a_mixed_dev_and_production_finding_stays_actionable() -> None:
    paths = [".>shadcn>braces", ".>next>braces"]
    (found,) = audit.pnpm_vulnerabilities(
        _pnpm_findings("<0.0.0", dev=True, paths=paths), _dev_roots()
    )
    assert not found.dev_only_no_fix
    assert audit.is_actionable([found], [])


def test_pnpm_audit_rejects_missing_advisories_map() -> None:
    with pytest.raises(TypeError, match="missing advisories"):
        audit.pnpm_vulnerabilities({})


def test_every_pinned_binary_constant_is_found_in_the_tree() -> None:
    for name, path, pattern in audit.PINS:
        assert audit.pinned_version(path, pattern), name


def test_report_runs_the_canonical_uv_pin(tmp_path: Path) -> None:
    commands = FakeCommands()
    commands.on(
        "uvx",
        Reply(
            stdout=json.dumps(
                {
                    "vulnerabilities": [
                        {
                            "dependency": {"name": "sample", "version": "1"},
                            "id": "GHSA-sample",
                            "display_id": "GHSA-sample",
                            "aliases": [],
                            "summary": "sample finding",
                            "fix_versions": ["2"],
                        }
                    ]
                }
            )
        ),
    )
    commands.on("pnpm", Reply(stdout='{"advisories": {}}'))
    pins = {name: audit.pinned_version(path, pattern) for name, path, pattern in audit.PINS}
    commands.on(
        ["gh", "api", "repos/pgvector/pgvector/tags?per_page=100"],
        Reply(stdout=json.dumps([{"name": "v" + pins["pgvector"]}])),
    )
    for repo, name in (
        ("wal-g/wal-g", "wal-g"),
        ("open-telemetry/opentelemetry-collector-releases", "otelcol-contrib"),
        ("astral-sh/uv", "uv"),
        ("pnpm/pnpm", "pnpm"),
        ("grafana/grafana", "Grafana"),
    ):
        commands.on(
            ["gh", "api", f"repos/{repo}/releases/latest"],
            Reply(stdout=json.dumps({"tag_name": pins[name]})),
        )
    opener = FakeUrlOpener()
    opener.on(
        "https://api.osv.dev/", Answer(json.dumps({"database_specific": {"severity": "HIGH"}}))
    )
    opener.on("https://repo1.maven.org/", Answer(f"<version>{pins['zonky Postgres']}</version>"))
    output = tmp_path / "report.md"
    assert audit.main(["--out", str(output)], run=commands, open_url=opener) == 0
    assert list(commands.argvs[0]) == [
        "uvx",
        "--from",
        f"uv=={UV_VERSION}",
        "uv",
        "audit",
        "--frozen",
        "--output-format",
        "json",
    ]
    report = output.read_text()
    assert report.startswith(audit.MARKER)
    assert "| high | sample |" in report
    for name in pins:
        assert f"| {name} |" in report
    assert all(call.timeout == 600 for call in commands.calls[:2])
    assert all(call.timeout == 60 for call in commands.calls[2:])
    assert [call.timeout for call in opener.calls] == [30, 30]


def test_zonky_latest_stays_on_the_pinned_major() -> None:
    metadata = "".join(f"<version>{v}</version>" for v in ("17.4.0", "17.11.0", "17.9.0", "18.6.0"))
    assert audit.zonky_latest("17.4.0", metadata) == "17.11.0"


def _binary(pinned: str, latest: str) -> audit.Binary:
    return audit.Binary("x", pinned, latest, "src")


def test_behind_compares_numerically_and_ignores_the_v_prefix() -> None:
    assert _binary("0.9.0", "v0.10.0").behind
    assert not _binary("v3.0.9", "v3.0.9").behind


def _vuln(severity: str) -> audit.Vulnerability:
    return audit.Vulnerability("python", "p", "1", "GHSA-x", severity, "2", "s")


def test_issue_is_actionable_on_high_unknown_or_a_behind_binary_only() -> None:
    current = [_binary("1.0", "1.0")]
    assert not audit.is_actionable([_vuln("moderate"), _vuln("low")], current)
    assert audit.is_actionable([_vuln("high")], current)
    assert audit.is_actionable([_vuln("unknown")], current)
    assert audit.is_actionable([], [_binary("1.0", "1.1")])


def test_report_orders_by_severity_and_carries_the_marker() -> None:
    report = audit.render([_vuln("low"), _vuln("critical")], [_binary("1.0", "1.1")])
    assert report.startswith(audit.MARKER)
    assert report.index("| critical |") < report.index("| low |")
    assert "| x | 1.0 | 1.1 | src | behind |" in report


class _FakeGh:
    def __init__(self, open_numbers: list[int]) -> None:
        self.calls: list[list[str]] = []
        self._open = open_numbers

    def __call__(self, argv: list[str]) -> str:
        self.calls.append(argv)
        if "--paginate" in argv:
            return "\n".join(map(str, self._open))
        if argv[:2] == ["issue", "create"]:
            return "https://github.com/o/r/issues/9\n"
        return ""


def test_actionable_run_updates_the_existing_issue_instead_of_opening_another() -> None:
    gh = _FakeGh([4])
    assert audit.sync_issue("o/r", "body", actionable=True, gh=gh) == "updated #4"
    assert not any(c[:2] == ["issue", "create"] for c in gh.calls)
    assert gh.calls[-1][:4] == ["api", "--method", "PATCH", "repos/o/r/issues/4"]


def test_actionable_run_with_no_issue_opens_one() -> None:
    gh = _FakeGh([])
    assert audit.sync_issue("o/r", "body", actionable=True, gh=gh).startswith("opened ")


def test_clean_run_closes_the_open_issue_and_a_clean_repo_does_nothing() -> None:
    gh = _FakeGh([4])
    assert audit.sync_issue("o/r", "body", actionable=False, gh=gh) == "closed 1 issue(s)"
    assert any("state=closed" in c for c in gh.calls)
    quiet = _FakeGh([])
    assert audit.sync_issue("o/r", "body", actionable=False, gh=quiet) == "closed 0 issue(s)"
    assert len(quiet.calls) == 1


def test_issue_lookup_filters_on_the_marker_and_skips_pull_requests() -> None:
    gh = _FakeGh([])
    audit.sync_issue("o/r", "body", actionable=False, gh=gh)
    jq = gh.calls[0][gh.calls[0].index("--jq") + 1]
    assert audit.MARKER in jq and 'has("pull_request") | not' in jq


def test_bare_stdlib_script_help_requires_no_repository_or_site_imports(tmp_path: Path) -> None:
    script = Path(audit.__file__).resolve()
    env = {
        key: value
        for key, value in os.environ.items()
        if key != "PYTHONPATH" and not key.startswith("MEIVA_")
    }
    result = run_bounded(
        [sys.executable, "-S", str(script), "--help"],
        timeout=10,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "Weekly dependency audit report" in result.stdout
    assert "--sync-issue" in result.stdout


def test_github_failure_preserves_command_error_before_report_or_http(tmp_path: Path) -> None:
    commands = FakeCommands()
    commands.on("uvx", Reply(stdout='{"vulnerabilities": []}'))
    commands.on("pnpm", Reply(stdout='{"advisories": {}}'))
    commands.on("gh", Reply(returncode=2, stdout="partial response", stderr="gh refused"))
    opener = FakeUrlOpener()
    output = tmp_path / "report.md"
    with pytest.raises(subprocess.CalledProcessError) as error:
        audit.main(["--out", str(output)], run=commands, open_url=opener)
    assert error.value.returncode == 2
    assert error.value.stdout == "partial response"
    assert error.value.stderr == "gh refused"
    assert error.value.cmd == list(commands.argvs[-1])
    assert commands.calls[-1].timeout == 60
    assert not output.exists()
    assert opener.calls == []
