import ast
import builtins
import io
import json
import os
import re
import shlex
import subprocess
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase

import yaml

from base.events.tests.codegen_sources import SourceGraph
from base.host.proc import run_bounded

ROOT = Path(__file__).resolve().parents[2]
CONFIG = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
HOOKS = {hook["id"]: hook for repo in CONFIG["repos"] for hook in repo["hooks"]}
CODEGEN = {hook_id for hook_id in HOOKS if hook_id.endswith("-fresh")}
JOB = WORKFLOW["jobs"]["backend-structure"]
STEPS = {step["name"]: step for step in JOB["steps"] if "name" in step}
LINT = STEPS["Run pre-commit structural gates (--all-files)"]
SELECT = STEPS["Select codegen freshness from changed files"]
FRESHNESS = STEPS["Run codegen freshness hooks (--all-files)"]
SCRIPT = SELECT["run"].split("<<'PY'\n", 1)[1].removesuffix("PY\n")
OTHER_CI = {
    "ruff",
    "ruff-format",
    "lint-migrations",
    "lint-clock-lattice",
    "lint-core-content-manifests",
}


CI_OWNED = {
    "check-code-comments",
    "pyright",
    "frontend-tsc",
    "frontend-eslint",
    "frontend-vitest",
    "meizel-check-changed",
}
PREPUSH = {
    "pyright",
    "frontend-tsc",
    "frontend-eslint-full",
    "frontend-vitest",
    "meizel-check-full",
}


# meiva:note
# `check-merge-conflict` is here twice in the config: the commit stage runs the upstream hook,
# which returns 0 unless the repository is mid-merge, so the push stage re-declares it with
# `--assume-in-merge`. This map is keyed by id, so the entry it holds is the push-stage one.
PREPUSH_LOCAL_ONLY = {
    "lint-prepush-branch-diff",
    "lint-prepush-artifact-freshness",
    "lint-tests-location-full",
    "check-merge-conflict",
    "diff-hints-branch",
}
LOCAL_ONLY = {"check-git-hooks-install"}


GENERATED_COMPONENTS = {
    "HTTPValidationError",
    "ValidationError",
    "Body_stage_upload_api_keyed_v1_agents__agent_id__uploads_staged_post",
}


def run_fixture(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = run_bounded(argv, timeout=30, cwd=cwd, env=env, capture_output=True, text=True)
    if check:
        result.check_returncode()
    return result


def assert_sources_covered(sources, pattern):
    missing = sorted(path for path in sources if not re.search(pattern, path))
    assert not missing, f"Uncovered codegen source modules: {missing}"


def test_openapi_component_sources_are_in_types_hook() -> None:
    components = json.loads((ROOT / "ui/web/openapi.json").read_text())["components"]["schemas"]
    assert components.keys() >= GENERATED_COMPONENTS
    sources = SourceGraph(ROOT).schema_sources(components, GENERATED_COMPONENTS)
    assert_sources_covered(sources, HOOKS["types-codegen-fresh"]["files"])


def test_event_contract_sources_are_in_events_hook() -> None:
    sources = SourceGraph(ROOT).imported_sources("base.events.contract")
    assert_sources_covered(sources, HOOKS["events-registry-fresh"]["files"])


def test_closure_follows_new_and_moved_definitions_without_name_collisions(tmp_path: Path) -> None:
    files = {
        "gateway/routers/probe.py": (
            "from gateway.schemas import WireModel\n"
            "@router.get('/probe')\ndef route() -> WireModel: ...\n"
        ),
        "gateway/schemas/__init__.py": "from gateway.schemas.probe import WireModel\n",
        "gateway/schemas/probe.py": "class WireModel: pass\n",
        "services/entrypoints/im_bridge/types.py": "class WireModel: pass\n",
        "base/new_model.py": "class NewModel: pass\n",
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    assert SourceGraph(tmp_path).schema_sources({"WireModel"}, set()) == {
        "gateway/schemas/probe.py"
    }

    (tmp_path / "gateway/schemas/probe.py").write_text(
        "from base.new_model import NewModel\nclass WireModel:\n    child: NewModel\n"
    )
    sources = SourceGraph(tmp_path).schema_sources({"WireModel"}, set())
    with TestCase().assertRaisesRegex(AssertionError, "base/new_model.py"):
        assert_sources_covered(sources, r"^gateway/")

    (tmp_path / "base/moved_model.py").write_text("class WireModel: pass\n")
    (tmp_path / "gateway/schemas/probe.py").write_text("from base.moved_model import WireModel\n")
    sources = SourceGraph(tmp_path).schema_sources({"WireModel"}, set())
    with TestCase().assertRaisesRegex(AssertionError, "base/moved_model.py"):
        assert_sources_covered(sources, r"^gateway/")


def test_closure_ignores_routes_declared_by_a_gateway_tests_directory(tmp_path: Path) -> None:
    files = {
        "gateway/routers/probe.py": (
            "from gateway.schemas import WireModel\n"
            "@router.get('/probe')\ndef route() -> WireModel: ...\n"
        ),
        "gateway/schemas/__init__.py": "from gateway.schemas.probe import WireModel\n",
        "gateway/schemas/probe.py": "class WireModel: pass\n",
        "gateway/tests/test_probe.py": (
            "class WireModel: pass\nclass FakeModel: pass\n"
            "@router.get('/fake')\ndef route() -> FakeModel: ...\n"
        ),
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    assert SourceGraph(tmp_path).schema_sources({"WireModel"}, set()) == {
        "gateway/schemas/probe.py"
    }


def test_events_closure_follows_payload_moves_outside_package(tmp_path: Path) -> None:
    sources = {
        "base/events/contract.py": "from .payloads import Spawn\n",
        "base/events/payloads.py": "from base.moved_payload import Spawn\n",
        "base/moved_payload.py": "class Spawn: pass\n",
    }
    for name, content in sources.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    paths = SourceGraph(tmp_path).imported_sources("base.events.contract")
    assert paths == sources.keys()
    with TestCase().assertRaisesRegex(AssertionError, "base/moved_payload.py"):
        assert_sources_covered(paths, r"^base/events/")


def test_closure_resolves_module_alias_reexports_before_regeneration(tmp_path: Path) -> None:
    sources = {
        "gateway/routers/probe.py": (
            "from gateway.schemas.probe import WireModel\n"
            "@router.get('/probe')\ndef route() -> WireModel: ...\n"
        ),
        "gateway/schemas/probe.py": (
            "from base import public_models\nclass WireModel:\n    child: public_models.NewModel\n"
        ),
        "base/__init__.py": "from . import private_models as public_models\n",
        "base/private_models.py": "class NewModel: pass\n",
    }
    for name, content in sources.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    paths = SourceGraph(tmp_path).schema_sources({"WireModel"}, set())
    with TestCase().assertRaisesRegex(AssertionError, "base/private_models.py"):
        assert_sources_covered(paths, r"^gateway/")


def test_closure_rejects_reachable_conditional_imports(tmp_path: Path) -> None:
    sources = {
        "gateway/routers/probe.py": (
            "from gateway.schemas.probe import WireModel\n"
            "@router.get('/probe')\ndef route() -> WireModel: ...\n"
        ),
        "gateway/schemas/probe.py": (
            "if flag:\n    from base.new_model import NewModel\nclass WireModel: pass\n"
        ),
        "base/new_model.py": "class NewModel: pass\n",
    }
    for name, content in sources.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    assert_sources_covered(SourceGraph(tmp_path).schema_sources({"WireModel"}, set()), r"^gateway/")
    schema = tmp_path / "gateway/schemas/probe.py"
    schema.write_text(
        schema.read_text().replace("class WireModel: pass", "class WireModel:\n    child: NewModel")
    )
    with TestCase().assertRaisesRegex(AssertionError, "Unsupported conditional schema binding"):
        SourceGraph(tmp_path).schema_sources({"WireModel"}, set())


def select(
    event: str,
    paths: tuple[str, ...] = (),
    *,
    error: Exception | None = None,
    yaml_config: dict[str, object] = CONFIG,
    yaml_missing: bool = False,
    read_error: Exception | None = None,
    yaml_error: Exception | None = None,
    re_error: Exception | None = None,
    open_error: Exception | None = None,
    fail_after_second_append: list[str] | None = None,
) -> tuple[str, str, list[list[str]]]:
    with TemporaryDirectory() as directory:
        output = Path(directory) / "output"
        env = {
            "EVENT_NAME": event,
            "GITHUB_OUTPUT": str(output),
        }
        if not isinstance(error, KeyError):
            env["LINT_STRUCTURE_BASELINE_BASE"] = "event-base-sha"
        stdout = io.StringIO()
        failed = False
        changed = b"\0".join(os.fsencode(path) for path in paths)
        calls: list[list[str]] = []
        appends = 0

        def check_output(cmd: list[str]) -> bytes:
            calls.append(cmd)
            if error is not None:
                raise error
            return changed

        def safe_load(_text: str) -> dict[str, object]:
            if yaml_error is not None:
                raise yaml_error
            return yaml_config

        def compile_regex(pattern: str) -> re.Pattern[str]:
            if re_error is not None:
                raise re_error
            return re.compile(pattern)

        class _SelectorPath:
            def __init__(self, raw: str) -> None:
                self._path = Path(raw)

            def read_text(self) -> str:
                if read_error is not None:
                    raise read_error
                return self._path.read_text()

            @contextmanager
            def open(self, mode: str = "r"):
                nonlocal appends
                if open_error is not None:
                    raise open_error
                with self._path.open(mode) as stream:
                    yield stream
                if mode == "a":
                    appends += 1
                    if appends == 2 and fail_after_second_append is not None:
                        fail_after_second_append.append(self._path.read_text())
                        raise OSError("close failed after publishing skip")

        def fake_import(name: str, *_args: object) -> object:
            if name == "os":
                return SimpleNamespace(environ=env, fsdecode=os.fsdecode)
            if name == "subprocess":
                return SimpleNamespace(check_output=check_output)
            if name == "yaml":
                if yaml_missing:
                    raise ModuleNotFoundError("No module named 'yaml'")
                return SimpleNamespace(safe_load=safe_load)
            if name == "re":
                return SimpleNamespace(compile=compile_regex)
            if name == "pathlib":
                return SimpleNamespace(Path=_SelectorPath)
            raise ImportError(name)

        ns: dict[str, object] = {
            "__builtins__": {**vars(builtins), "__import__": fake_import},
        }
        with redirect_stdout(stdout):
            try:
                exec(compile(SCRIPT, "ci.yml codegen selector", "exec"), ns)
            except SystemExit as exit_error:
                assert exit_error.code == 1
                failed = True

        decision = output.read_text().splitlines()[-1] if output.exists() else "run=true"
        if failed:
            decision = "run=true"
        return decision + "\n", stdout.getvalue(), calls


def test_required_job_identity_and_unconditional_lint() -> None:
    assert JOB["name"] == "backend structure (pre-commit lint + codegen freshness)"
    assert JOB["timeout-minutes"] == 25
    assert JOB["needs"] == ["classify"]
    assert JOB["if"] == "${{ needs.classify.result == 'success' }}"
    assert "if" not in LINT
    assert "continue-on-error" not in JOB
    assert "continue-on-error" not in LINT
    assert LINT["run"] == "env -u VIRTUAL_ENV uv run pre-commit run --all-files"


def test_codegen_invokes_exactly_the_four_configured_hooks() -> None:
    assert {
        "types-codegen-fresh",
        "constants-codegen-fresh",
        "events-registry-fresh",
        "config-lite-table-fresh",
    } == CODEGEN
    commands = [shlex.split(line) for line in FRESHNESS["run"].splitlines()]
    assert len(commands) == len(CODEGEN)
    assert all(
        command[:6] == ["env", "-u", "VIRTUAL_ENV", "uv", "run", "pre-commit"]
        for command in commands
    )
    assert all(command[6] == "run" and command[8:] == ["--all-files"] for command in commands)
    invoked = [command[7] for command in commands]
    assert set(invoked) == CODEGEN

    assert "SKIP" not in JOB["env"]
    assert "SKIP" not in FRESHNESS.get("env", {})
    assert "continue-on-error" not in FRESHNESS


def test_four_hooks_exactly_partition_the_existing_structure_gate() -> None:
    skipped = set(LINT["env"]["SKIP"].split(","))
    assert skipped == OTHER_CI | CI_OWNED | LOCAL_ONLY | CODEGEN
    commit_hooks = {
        hook_id
        for hook_id, hook in HOOKS.items()
        if "pre-commit" in hook.get("stages", CONFIG["default_stages"])
    }
    lint_hooks = commit_hooks - skipped
    assert not lint_hooks & CODEGEN
    assert lint_hooks | CODEGEN == commit_hooks - OTHER_CI - LOCAL_ONLY - CI_OWNED


def test_selector_ids_and_regexes_follow_hook_config() -> None:
    assignments = {
        node.targets[0].id: node.value
        for node in ast.walk(ast.parse(SCRIPT))
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
    }
    assert set(ast.literal_eval(assignments["codegen_ids"])) == CODEGEN
    assert ".pre-commit-config.yaml" in SCRIPT
    assert 'hooks[hook_id]["files"]' in SCRIPT
    for path in ("base/events/", "ui/web/openapi.json", "base/config/"):
        assert path in SCRIPT


def test_parser_is_unconditional_and_codegen_retains_conservative_selection() -> None:
    assert SELECT["id"] == "codegen"
    assert SELECT["env"]["EVENT_NAME"] == "${{ github.event_name }}"
    assert SELECT["continue-on-error"] is True
    for name in (
        "Install Node",
        "Install frontend deps (source parser and codegen)",
        "Check source notes and changed-file migration",
    ):
        assert "if" not in STEPS[name]
        assert "continue-on-error" not in STEPS[name]
    assert (
        FRESHNESS["if"]
        == "steps.codegen.outcome != 'success' || steps.codegen.outputs.run != 'false'"
    )
    assert (
        STEPS["Check source notes and changed-file migration"]["run"]
        == 'bash scripts/content_lint/code_comments/check.sh changed "${LINT_STRUCTURE_BASELINE_BASE:?missing comparison base}"'
    )
    assert HOOKS["check-code-comments"]["always_run"] is True
    assert HOOKS["check-code-comments"]["pass_filenames"] is False
    order = list(STEPS)
    assert order.index("Fetch base revision for structure guard") < order.index(
        "Run pre-commit structural gates (--all-files)"
    )
    assert order.index("Run pre-commit structural gates (--all-files)") < order.index(
        "Select codegen freshness from changed files"
    )
    assert (
        order.index("Install Node")
        < order.index("Check source notes and changed-file migration")
        < order.index(LINT["name"])
    )


def test_structure_guard_pins_the_merge_refs_base_not_the_event_field() -> None:

    script = STEPS["Fetch base revision for structure guard"]["run"]
    deepen = 'git fetch --depth=2 origin "$(git rev-parse HEAD)"'
    resolve = "git rev-parse --verify --quiet 'HEAD^1^{commit}'"
    fallback = 'base="${{ github.event.pull_request.base.sha }}"'
    base_fetch = 'git fetch --depth=1 origin "$base"'
    for needle in (deepen, resolve, fallback, base_fetch):
        assert needle in script
    assert (
        script.index(deepen)
        < script.index(resolve)
        < script.index(fallback)
        < script.index(base_fetch)
    )
    assert "git rev-parse --verify --quiet HEAD^2" in script
    assert "LINT_STRUCTURE_BASELINE_BASE" in script


def test_shallow_checkout_needs_the_deepen_before_resolving_head_parent(
    tmp_path: Path,
) -> None:

    source = tmp_path / "source"
    source.mkdir()

    def git(*args: str, cwd: Path = source) -> str:
        return run_fixture(["git", *args], cwd=cwd, check=True).stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.email", "structure-guard@example.invalid")
    git("config", "user.name", "structure-guard")

    git("config", "uploadpack.allowAnySHA1InWant", "true")
    (source / "a").write_text("a\n")
    git("add", "a")
    git("commit", "-qm", "base one")
    git("checkout", "-qb", "feature")
    (source / "b").write_text("b\n")
    git("add", "b")
    git("commit", "-qm", "feature head")
    git("checkout", "-q", "main")
    (source / "m").write_text("m\n")
    git("add", "m")
    git("commit", "-qm", "base two")
    git("merge", "-q", "--no-ff", "feature", "-m", "merge feature")
    base = git("rev-parse", "HEAD^1")

    checkout = tmp_path / "checkout"
    run_fixture(
        ["git", "clone", "-q", "--depth=1", source.as_uri(), str(checkout)],
        check=True,
    )
    literal = run_fixture(
        ["git", "rev-parse", "HEAD^1"],
        cwd=checkout,
        check=False,
    )
    assert literal.returncode != 0
    assert literal.stdout.strip() == "HEAD^1"
    silent = run_fixture(
        ["git", "rev-parse", "--verify", "--quiet", "HEAD^1^{commit}"],
        cwd=checkout,
        check=False,
    )
    assert silent.returncode != 0
    assert silent.stdout.strip() == ""
    merge = git("rev-parse", "HEAD", cwd=checkout)
    run_fixture(
        ["git", "fetch", "-q", "--depth=2", "origin", merge],
        cwd=checkout,
        check=True,
    )
    assert git("rev-parse", "HEAD^1", cwd=checkout) == base


def test_manual_structure_guard_resolves_shallow_source_ancestry(tmp_path: Path) -> None:

    step = STEPS["Fetch comparison ancestry for manual structure guard"]
    assert step["if"] == "github.event_name == 'workflow_dispatch'"
    names = list(STEPS)
    assert names.index(step["name"]) < names.index(LINT["name"])
    source = tmp_path / "source"
    source.mkdir()

    def git(*args: str, cwd: Path = source) -> str:
        return run_fixture(["git", *args], cwd=cwd, check=True).stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.email", "structure-guard@example.invalid")
    git("config", "user.name", "structure-guard")
    git("commit", "--allow-empty", "-qm", "measured source")
    common = git("rev-parse", "HEAD")
    git("checkout", "-qb", "snapshot")
    git("commit", "--allow-empty", "-qm", "refreshed timings")
    git("checkout", "-q", "main")
    git("commit", "--allow-empty", "-qm", "main moved")
    checkout = tmp_path / "checkout"
    git("clone", "-q", "--depth=1", "--branch", "snapshot", source.as_uri(), str(checkout))
    assert git("rev-parse", "--is-shallow-repository", cwd=checkout) == "true"
    output = tmp_path / "env"
    run_fixture(
        ["bash", "-e", "-c", step["run"]],
        cwd=checkout,
        env={**os.environ, "GITHUB_ENV": str(output)},
        check=True,
    )
    assert output.read_text().strip() == f"LINT_STRUCTURE_BASELINE_BASE={common}"
    assert git("rev-parse", "--is-shallow-repository", cwd=checkout) == "false"


def test_every_codegen_input_family_selects_freshness() -> None:
    paths = (
        "scripts/codegen/typescript-types.mjs",
        "scripts/codegen/codegen-types.sh",
        "scripts/codegen/check-types-fresh.sh",
        "scripts/codegen/dump_openapi.py",
        "gateway/schemas/tasks.py",
        "gateway/agents/schemas.py",
        "gateway/app.py",
        "gateway/routers/tasks.py",
        "gateway/agents/router.py",
        "base/agents/model/birth_config.py",
        "base/api_contracts/agent_contract.py",
        "base/api_contracts/contracts.py",
        "base/agents/tasks/priority.py",
        "base/agents/tasks/status.py",
        "base/agents/history/timeline.py",
        "base/agents/observation/roster.py",
        "base/agents/observation/evidence.py",
        "base/agents/history/timeline_item.py",
        "ops/rpc_schemas/messages.py",
        "ops/cluster_status/__init__.py",
        "base/agents/impersonation/history.py",
        "base/agents/sdk/telemetry.py",
        "base/agents/observation/snapshot.py",
        "base/host/resource/reading.py",
        "ops/rpc_schemas/__init__.py",
        "ops/rpc_schemas/terminate.py",
        "ui/web/src/lib/contracts/types-generated.ts",
        "ui/web/openapi.json",
        "base/events/live/projection.py",
        "scripts/codegen/dump_frontend_constants.py",
        "ui/web/src/lib/contracts/constants-generated.ts",
        "base/events/contract.py",
        "base/events/vocabulary.py",
        "base/events/loader.py",
        "base/events/declarations/__init__.py",
        "base/events/declarations/host.py",
        "scripts/codegen/gen_event_registry.py",
        "base/events/registry.md",
        "base/config/config_registry.py",
        "base/config/domains/agent/settings.py",
        "scripts/codegen/gen_config_lite_table.py",
        "base/host/env/config_lite_table.json",
    )
    for path in paths:
        assert (ROOT / path).is_file(), f"Freshness matrix path no longer exists: {path}"
        assert any(re.search(HOOKS[hook_id]["files"], path) for hook_id in CODEGEN), path

        output, log, calls = select("pull_request", (path,))
        assert output == "run=true\n", path
        assert "STEP SKIPPED" not in log
        assert calls == [
            [
                "git",
                "diff",
                "--name-only",
                "--no-renames",
                "-z",
                "event-base-sha",
                "HEAD",
            ]
        ]


def test_irrelevant_and_empty_diffs_skip_visibly() -> None:
    for paths in ((), ("db/schema.sql", "docs/conventions/runbook.md", "ui/web/src/app/page.tsx")):
        output, log, _ = select("pull_request", paths)
        assert output == "run=false\n"
        assert log.splitlines() == [
            "STEP SKIPPED: codegen freshness — no codegen inputs changed in this PR; "
            "manual dispatch always runs it; the merge queue re-runs the combined tree."
        ]


def test_manual_dispatch_always_runs_without_diff() -> None:
    output, log, calls = select("workflow_dispatch")
    assert output == "run=true\n" and "STEP SKIPPED" not in log
    assert not calls


def test_diff_and_shallow_history_failures_run_freshness() -> None:
    for error in (
        subprocess.CalledProcessError(128, "git diff", stderr=b"fatal: bad object"),
        FileNotFoundError("git"),
        KeyError("LINT_STRUCTURE_BASELINE_BASE"),
    ):
        output, log, _ = select("pull_request", error=error)
        assert output == "run=true\n"
        assert "::warning::Codegen selector failed" in log
        assert "STEP SKIPPED" not in log


def test_selector_config_and_runtime_failures_run_freshness() -> None:
    paths = ("base/agents/observation/roster.py",)
    for output, log, _ in (
        select("pull_request", paths, read_error=PermissionError("config unreadable")),
        select("pull_request", paths, yaml_error=yaml.YAMLError("invalid YAML")),
        select("pull_request", paths, re_error=re.error("invalid regex")),
        select("pull_request", paths, re_error=RuntimeError("unexpected selector error")),
        select("pull_request", paths, open_error=OSError("output unavailable")),
    ):
        assert output == "run=true\n"
        assert "::warning::Codegen selector failed" in log
        assert "STEP SKIPPED" not in log


def test_missing_hook_defaults_to_run() -> None:
    output, log, _ = select(
        "pull_request",
        ("base/agents/observation/roster.py",),
        yaml_config={"repos": []},
    )
    assert output == "run=true\n"
    assert "::warning::Codegen selector failed (KeyError)" in log


def test_failure_after_skip_output_is_written_still_runs_freshness() -> None:
    written: list[str] = []
    output, log, _ = select("pull_request", fail_after_second_append=written)
    assert written == ["run=true\nrun=false\n"]
    assert output == "run=true\n"
    assert "::warning::Codegen selector failed (OSError)" in log
    assert "STEP SKIPPED" not in log


def test_selector_import_failure_defaults_to_run() -> None:
    output, log, _ = select(
        "pull_request",
        ("base/agents/observation/roster.py",),
        yaml_missing=True,
    )
    assert output == "run=true\n"
    assert "::warning::Codegen selector failed (ModuleNotFoundError)" in log


def test_prepush_migration_keeps_direct_ci_owners(tmp_path: Path) -> None:
    assert {
        hook_id for hook_id, hook in HOOKS.items() if hook.get("stages") == ["pre-push"]
    } == PREPUSH | PREPUSH_LOCAL_ONLY
    for hook_id in PREPUSH_LOCAL_ONLY:
        assert HOOKS[hook_id]["always_run"] is True
    backend = WORKFLOW["jobs"]["backend-static"]["steps"]
    frontend = WORKFLOW["jobs"]["frontend"]["steps"]
    command = next(step["run"] for step in backend if step.get("name") == "Run pyright")
    assert command == "time uv run basedpyright --threads 2"
    uv = tmp_path / "uv"
    uv.write_text("#!/bin/sh\nexit 19\n")
    uv.chmod(0o755)
    failed = run_fixture(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", command],
        env=os.environ | {"PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"},
        check=False,
    )
    assert failed.returncode == 19
    assert any(step.get("run") == "pnpm exec tsc --noEmit" for step in frontend)
    assert any(step.get("run") == "pnpm run lint" for step in frontend)
    assert any(
        step.get("run") == "pnpm exec vitest run --config tests/vitest.config.mts --coverage"
        for step in frontend
    )
