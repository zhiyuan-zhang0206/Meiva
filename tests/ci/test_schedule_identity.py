# meiva:note
# A schedule's identity is bound by the runtime, never asserted by the script.
#
# A gateway schedule that writes as an agent -- a task card, a message -- must carry an identity the
# runner bound for it: `schedules.owner_agent_id` reaches the process as `MEIVA_AGENT_ID` and the
# actor stays `schedule:<id>`. A script that asserts its own identity breaks the only trust root the
# downstream reader has, because `system:<any name>` is indistinguishable at the receiving end from
# a name the runtime assigned. Self-asserted identity is worse than no identity: it makes the two
# look alike.
#
# Two shapes are refused here, both read from the AST: a call to `establish_actor` (minting an
# identity in-process) and a `system:` string literal (a provenance claimed out of thin air -- the
# CLI's `--source` fallback). The truthful value for a schedule is `schedule:<id>`. Synthetic
# sources prove the scan still matches what it rejects, so a refactor cannot leave it green on
# nothing.

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BANNED_CALL = "establish_actor"
_BANNED_PREFIX = "system:"


# meiva:note
# The tracked Python sources under `schedules/`, named by git rather than by walking the directory:
# the guard must judge what the repository actually carries, and S603 is waived because the argv is
# a fixed git query with no caller-supplied element in it.
def _tracked_sources() -> list[Path]:
    result = subprocess.run(  # noqa: S603
        ["git", "-C", str(_REPO_ROOT), "ls-files", "-z", "--", "schedules"],
        capture_output=True,
        text=True,
        check=True,
    )
    paths = [line for line in result.stdout.split("\0") if line.endswith(".py")]
    return [_REPO_ROOT / path for path in paths]


# meiva:note
# Every self-asserted identity in one module, as `name:line: reason` strings. A schedule that does
# not parse is #24's red, not this test's, so it reports as a finding rather than raising.
def _findings(name: str, source: str) -> list[str]:
    try:
        tree = ast.parse(source, filename=name)
    except SyntaxError as exc:
        return [f"{name}:{exc.lineno}: does not parse"]
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if called == _BANNED_CALL:
                found.append(f"{name}:{node.lineno}: calls {_BANNED_CALL}()")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.startswith(_BANNED_PREFIX):
                found.append(f"{name}:{node.lineno}: claims a {_BANNED_PREFIX} provenance")
    return found


def test_schedule_sources_never_assert_their_own_identity() -> None:
    sources = _tracked_sources()
    assert sources, "git ls-files found no schedule sources -- the guard would pass on nothing"
    findings = [
        finding
        for path in sources
        for finding in _findings(
            str(path.relative_to(_REPO_ROOT)), path.read_text(encoding="utf-8")
        )
    ]
    assert findings == [], "a schedule asserted its own identity:\n" + "\n".join(findings)


def test_the_scan_sees_an_asserted_identity() -> None:
    source = "import agent_identity\n\nagent_identity.establish_actor('system:x')\n"
    findings = _findings("synthetic.py", source)
    assert len(findings) == 2, findings
    assert "calls establish_actor()" in findings[0]
    assert "claims a system: provenance" in findings[1]


def test_the_scan_sees_a_cli_provenance_literal() -> None:
    source = 'ARGS = ["--source", "system:sched-dry-import"]\n'
    assert _findings("synthetic.py", source) == ["synthetic.py:1: claims a system: provenance"]


def test_the_scan_leaves_a_bound_schedule_alone() -> None:
    source = 'import meiva\n\nmeiva.agents.send_message(42, "hello")\n'
    assert _findings("synthetic.py", source) == []
