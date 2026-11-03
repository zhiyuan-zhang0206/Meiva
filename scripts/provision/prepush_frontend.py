from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

# meiva:note
# A standalone script: the repository root goes on the path before the first import of the
# repository's own packages.
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from base.host.env.boot_actions import enter_scratch_home  # noqa: E402

if __name__ == "__main__":
    enter_scratch_home()

from base.host.boundary.process import stderr_text  # noqa: E402

_SCRIPTS = _ROOT / "scripts"
_WEB = "ui/web/"
_CODE = (".ts", ".tsx", ".js", ".mjs", ".mts")
# meiva:note
# _TYPE_INPUTS names the canonical schema/type artifacts without runtime imports (openapi.json, types-generated.ts): their consumers are proven by project tsc and the codegen-freshness hook, so a change confined to them schedules no related Vitest run while a changed runtime source still does.
# meiva:link scripts/provision/docs/prepush-selection.meiva.okf.md
_TYPE_INPUTS = {"ui/web/openapi.json", "ui/web/src/lib/contracts/types-generated.ts"}
_GLOBAL = {
    "ui/web/package.json",
    "ui/web/pnpm-lock.yaml",
    "ui/web/pnpm-workspace.yaml",
    "ui/web/tsconfig.json",
    "ui/web/tests/vitest.config.mts",
    "ui/web/tests/vitest.flaky.config.mts",
    "ui/web/tests/vitest.setup.ts",
    "ui/web/eslint.config.mjs",
    "ui/web/next.config.ts",
    "ui/web/postcss.config.mjs",
}
# meiva:note
# _DISK_CONSUMERS pairs each input read from disk outside Vitest's import graph (fixtures, locale files, css, layout, gate html) with the contract test that proves it; a new out-of-graph reader must join the table or its push-stage verification silently disappears. The input side is matched by prefix against the contribution paths.
# meiva:link scripts/provision/docs/prepush-selection.meiva.okf.md
_DISK_CONSUMERS = (
    ("ui/web/src/", "src/lib/auth/localstorage-policy.test.ts"),
    ("ui/web/messages/", "src/i18n/messages-layout.test.ts"),
    ("ui/web/src/app/globals.css", "src/app/globals-font-stack.test.ts"),
    ("ui/web/src/app/globals.css", "src/app/color-scheme.test.ts"),
    ("ui/web/src/app/layout.tsx", "src/app/color-scheme.test.ts"),
    ("ui/web/package.json", "src/lib/transport/frontend-bind.test.ts"),
    ("ui/app/app-ui/", "src/app/app-ui-locale.test.ts"),
    ("tests/fixtures/events/", "src/lib/contracts/event-fixtures.test.ts"),
    ("services/entrypoints/gate/static/login.html", "src/lib/transport/gate-login.test.ts"),
    (
        "base/packages/plugins/surface/ui_contributions.py",
        "src/components/plugins/plugin-nav-icon.test.ts",
    ),
)
_RSC_MARKERS = ("next/navigation", "next/headers", '"use client"', "'use client'")
# meiva:note
# An unusable frontend environment is a skip, not a tool status: `prepush-guard.sh` already answers
# a missing tree that way, and a push that cannot check the frontend is not a red lint. The two
# outcomes stay distinguishable by what they say and by the exit status they carry.
_FRONTEND_FIX = "cd ui/web && pnpm install --frozen-lockfile"
_PROBE_TIMEOUT_S = 120
_VERSION = re.compile(r"v?\d+(?:\.\d+)+")


# meiva:link scripts/provision/docs/prepush-selection.meiva.okf.md
def _query(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


# meiva:note
# The selection range is the branch contribution — merge-base(origin/main, HEAD)..HEAD, resolved by scripts/hooks/prepush-base.sh — never an old remote push tip. Renames and deletions keep both sides visible: --no-renames splits a rename into its deletion and addition. An unknown range is an error, never an empty change set.
# meiva:link scripts/provision/docs/prepush-selection.meiva.okf.md
def contribution() -> list[str]:
    base = subprocess.run(
        ["bash", str(_SCRIPTS / "hooks" / "prepush-base.sh")],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return _query("diff", "--name-only", "--no-renames", "-z", base, "HEAD").split("\0")[:-1]


def _run(tool: str, command: list[str]) -> int:
    return subprocess.run(
        ["bash", str(_SCRIPTS / "hooks" / "prepush-guard.sh"), tool, "--", *command], check=False
    ).returncode


# meiva:note
# Vitest's `related` mode defaults passWithNoTests=true; the selector overrides it to false: an empty affected collection is missing evidence, never a passing test run.
# meiva:link scripts/provision/docs/prepush-selection.meiva.okf.md
def _vitest(paths: list[str], *, related: bool = False) -> int:
    return _run(
        "vitest",
        [
            "bash",
            "-c",
            'cd ui/web && exec pnpm exec vitest "$@"',
            "vitest",
            *(["related", "--run"] if related else ["run"]),
            "--config",
            "tests/vitest.config.mts",
            "--passWithNoTests=false",
            *paths,
        ],
    )


def _eslint(existing: list[str]) -> int:
    files = [p.removeprefix(_WEB) for p in existing if p.endswith(_CODE)]
    if not files:
        print(
            "WARNING: PRE-PUSH UNVERIFIED [eslint]: no surviving code paths; global/deletion effects require CI",
            file=sys.stderr,
        )
        return 0
    return _run(
        "eslint",
        [
            "bash",
            "-o",
            "pipefail",
            "-c",
            'cd ui/web && pnpm exec eslint --no-warn-ignored --max-warnings 0 "$@"',
            "eslint",
            *files,
        ],
    )


def _tests(web: list[str], existing: list[str], disk: list[str]) -> int:
    if any(p in _GLOBAL or not Path(p).is_file() for p in web):
        print(
            "WARNING: PRE-PUSH UNVERIFIED [vitest]: global configuration/deleted-input closure requires CI",
            file=sys.stderr,
        )
    direct = _direct_tests(existing, disk)
    sources = _related_sources(existing, direct)
    if direct:
        status = _vitest(direct)
        if status:
            return status
    if sources:
        return _vitest(sources, related=True)
    if not direct:
        if existing and all(p in _TYPE_INPUTS for p in existing):
            print(
                "pre-push: schema/type artifacts require tsc and codegen freshness; no runtime Vitest input"
            )
            return 0
        print(
            "WARNING: PRE-PUSH UNVERIFIED [vitest]: no locally selectable consumer; affected verification requires CI",
            file=sys.stderr,
        )
    return 0


def _direct_tests(existing: list[str], disk: list[str]) -> list[str]:
    return sorted(
        set(disk) | {p.removeprefix(_WEB) for p in existing if ".test." in p or ".spec." in p}
    )


def _related_sources(existing: list[str], direct: list[str]) -> list[str]:
    return [
        p.removeprefix(_WEB)
        for p in existing
        if p not in _GLOBAL
        and p not in _TYPE_INPUTS
        and (p.endswith(_CODE) or p.endswith(".json"))
        and p.removeprefix(_WEB) not in direct
    ]


# meiva:note
# A push cannot check the frontend when `ui/web` was installed some other way, and reporting that as
# a failed lint sends the reader to code that is fine: on an `npm install` tree `pnpm exec eslint`
# answers `Command "eslint" not found`, which reads like a defect in the change (#6021).
#
# The verdict is functional -- run the tool `pnpm-lock.yaml` pins -- because a hand-assembled
# `node_modules` satisfies every structural signal while holding none of the tools; structure only
# chooses which explanation to print. The exit status is not consulted on its own: pnpm answered
# that failed probe with status 0 in this repository, so the answered version is what counts.
# meiva:link scripts/provision/docs/prepush-selection.meiva.okf.md
def _environment_verdict() -> str | None:
    try:
        probe = subprocess.run(
            ["pnpm", "exec", "eslint", "--version"],
            cwd=_WEB,
            capture_output=True,
            text=True,
            check=False,
            timeout=_PROBE_TIMEOUT_S,
        )
    except FileNotFoundError:
        return "pnpm is not on PATH, so no frontend check can run here"
    except subprocess.TimeoutExpired:
        return f"`pnpm exec eslint --version` did not answer within {_PROBE_TIMEOUT_S}s"
    if probe.returncode == 0 and _answered_a_version(probe.stdout):
        return None
    installed = Path(_WEB, "node_modules")
    if not installed.is_dir():
        return "ui/web/node_modules is missing, so the frontend tools are not installed"
    if not (installed / ".pnpm").is_dir():
        return "ui/web/node_modules has no .pnpm store, so something other than pnpm installed it"
    return "pnpm cannot run the tools the lockfile pins from this ui/web/node_modules"


# meiva:note
# A version on any line, not the whole output: pnpm prints its own notices (a newer release is
# available) around the tool's answer, and treating those as an unusable tree would block a push
# that can be checked. A failed probe answers no version at all, which is what separates the two.
def _answered_a_version(stdout: str) -> bool:
    return any(_VERSION.fullmatch(line.strip()) for line in stdout.splitlines())


# meiva:note
# The message carries the reason, not only the command: without it the next reader installs the
# same tree again. It also says what did not happen, in the words the guard already uses, because
# a silently unchecked frontend reads as a passing one.
def _report_environment(tool: str, verdict: str) -> None:
    print(
        f"WARNING: PRE-PUSH SKIPPED [{tool}]: {verdict}. CI must pass before merge.",
        file=sys.stderr,
    )
    print(
        "ui/web is installed with pnpm: pnpm-lock.yaml pins its graph, and an `npm install` tree has "
        "no node_modules/.pnpm and hoists dependencies differently, while a hand-assembled one can "
        f"look populated and still hold none of the tools. Fix: {_FRONTEND_FIX}",
        file=sys.stderr,
    )


def main(tool: str) -> int:
    paths = contribution()
    web = [p for p in paths if p.startswith(_WEB)]
    disk = sorted(
        {test for prefix, test in _DISK_CONSUMERS if any(p.startswith(prefix) for p in paths)}
    )
    if not web and (tool != "vitest" or not disk):
        print(f"pre-push: no {tool} input in branch contribution; no tool invoked")
        return 0
    if verdict := _environment_verdict():
        _report_environment(tool, verdict)
        return 0
    if tool == "tsc":
        return _run(
            tool,
            [
                "bash",
                "-c",
                "cd ui/web && pnpm exec next typegen && pnpm exec tsc --noEmit",
            ],
        )
    existing = [p for p in web if Path(p).is_file()]
    if tool == "eslint":
        return _eslint(existing)
    return _tests(web, existing, disk)


def _run_ci_pnpm(*args: str) -> int:
    return subprocess.run(["pnpm", *args], cwd="ui/web", check=False).returncode


def _touches_rsc_or_build(web: list[str], existing: list[str]) -> bool:
    if any(p in _GLOBAL or p.startswith("ui/web/src/app/") or not Path(p).is_file() for p in web):
        return True
    for rel in existing:
        if not rel.endswith((".ts", ".tsx")):
            continue
        try:
            text = Path(rel).read_text(encoding="utf-8")
        except OSError:
            continue
        if any(marker in text for marker in _RSC_MARKERS):
            return True
    return False


# meiva:note
# Incremental pull-request CI passes `--base <sha>` directly so execution never consults the host
# load guard or local lockfile, and widens to full ESLint, Vitest and Next.js build checks whenever
# global configuration, local ESLint rules, RSC boundaries or file deletions appear in the diff.
# meiva:link scripts/provision/docs/prepush-selection.meiva.okf.md
def run_ci_incremental(base: str) -> int:
    paths = _query("diff", "--name-only", "--no-renames", "-z", base, "HEAD").split("\0")[:-1]
    web = [p for p in paths if p.startswith(_WEB)]
    disk = sorted(
        {test for prefix, test in _DISK_CONSUMERS if any(p.startswith(prefix) for p in paths)}
    )
    existing = [p for p in web if Path(p).is_file()]
    full = any(p in _GLOBAL or not Path(p).is_file() for p in web)
    return _first_failure(
        (
            *_lint_commands(web, existing, full=full),
            *_build_commands(web, existing),
            *_test_commands(existing, disk, full=full),
        )
    )


# meiva:note
# One command per step and the first failure wins: each step below decides only which `pnpm`
# invocations it contributes, so the sequence stays readable as the three phases a push runs.
def _first_failure(commands: Iterable[tuple[str, ...]]) -> int:
    for command in commands:
        if status := _run_ci_pnpm(*command):
            return status
    return 0


def _lint_commands(web: list[str], existing: list[str], *, full: bool) -> list[tuple[str, ...]]:
    if full or any(p.startswith("ui/web/eslint-rules/") for p in web):
        return [("run", "lint")]
    code_files = [p.removeprefix(_WEB) for p in existing if p.endswith(_CODE)]
    if not code_files:
        return []
    return [("exec", "eslint", "--no-warn-ignored", "--max-warnings", "0", *code_files)]


def _build_commands(web: list[str], existing: list[str]) -> list[tuple[str, ...]]:
    if not _touches_rsc_or_build(web, existing):
        return []
    return [("run", "build"), ("run", "check:bundle-integrity")]


def _test_commands(existing: list[str], disk: list[str], *, full: bool) -> list[tuple[str, ...]]:
    if full:
        return [
            ("exec", "vitest", "run", "--config", "tests/vitest.config.mts"),
            ("exec", "vitest", "run", "--config", "tests/vitest.flaky.config.mts"),
        ]
    direct = [t for t in _direct_tests(existing, disk) if "/flaky/" not in f"/{t}"]
    sources = _related_sources(existing, direct)
    commands: list[tuple[str, ...]] = []
    if direct:
        commands.append(
            (
                "exec",
                "vitest",
                "run",
                "--config",
                "tests/vitest.config.mts",
                "--passWithNoTests=false",
                *direct,
            )
        )
    if sources:
        commands.append(
            (
                "exec",
                "vitest",
                "related",
                "--run",
                "--config",
                "tests/vitest.config.mts",
                "--passWithNoTests",
                *sources,
            )
        )
    if any("/flaky/" in p for p in existing):
        commands.append(("exec", "vitest", "run", "--config", "tests/vitest.flaky.config.mts"))
    return commands


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--base" and sys.argv[2]:
        try:
            raise SystemExit(run_ci_incremental(sys.argv[2]))
        except subprocess.CalledProcessError as error:
            print(stderr_text(error) or str(error), file=sys.stderr)
            raise SystemExit(error.returncode) from None
    if len(sys.argv) != 2 or sys.argv[1] not in {"tsc", "eslint", "vitest"}:
        raise SystemExit("usage: prepush_frontend.py (tsc|eslint|vitest | --base <sha>)")
    try:
        raise SystemExit(main(sys.argv[1]))
    except subprocess.CalledProcessError as error:
        print(stderr_text(error) or str(error), file=sys.stderr)
        raise SystemExit(error.returncode) from None
