from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

HINTS = Path(__file__).resolve().parents[1] / "hints.py"
LOCATOR = "def inputs(request):\n    return request.app.state.thing\n"
ROOT_TABLE = '[roots]\n"x.py" = { kinds = ["settings"], why = "process entry" }\n'


# meiva:note
# Fixed argument vectors in a temporary repository: git from PATH, never a shell.
def git(repo: Path, *args: str) -> str:
    done = subprocess.run(  # noqa: S603
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def commit(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-verify", "-m", message)


# meiva:note
# The hook script as a real child process, in the temporary repository.
def run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, str(HINTS), *args], cwd=repo, capture_output=True, text=True, check=False
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    (root / "cli").mkdir(parents=True)
    git(tmp_path, "init", "-q", "-b", "main", str(root))
    git(root, "config", "user.name", "test")
    git(root, "config", "user.email", "test@meiva")
    (root / "cli" / "x.py").write_text("VALUE = 1\n", encoding="utf-8")
    commit(root, "base")
    git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    return root


def message_file(repo: Path, text: str) -> str:
    path = repo / ".git" / "COMMIT_EDITMSG"
    path.write_text(text, encoding="utf-8")
    return str(path)


def stage_locator(repo: Path) -> None:
    (repo / "cli" / "x.py").write_text(LOCATOR, encoding="utf-8")
    git(repo, "add", "cli/x.py")


def test_an_unanswered_hint_blocks_the_commit_message(repo: Path) -> None:
    stage_locator(repo)
    done = run(repo, "commit-msg", message_file(repo, "Fetch from the application\n"))
    assert done.returncode == 1
    assert "cli/x.py:2: hint service-locator" in done.stderr
    assert "why:" in done.stderr and "instead:" in done.stderr and "see:" in done.stderr
    assert "Hint-Ack: service-locator cli/x.py:2 -- <reason>" in done.stderr


def test_a_line_trailer_answers_the_commit(repo: Path) -> None:
    stage_locator(repo)
    message = "Fetch from the application\n\nHint-Ack: service-locator cli/x.py:2 -- composition root of the daemon\n"
    done = run(repo, "commit-msg", message_file(repo, message))
    assert done.returncode == 0, done.stderr


def test_a_path_trailer_answers_every_site_in_the_file(repo: Path) -> None:
    stage_locator(repo)
    message = "Fetch from the application\n\nHint-Ack: service-locator cli/x.py -- composition root of the daemon\n"
    assert run(repo, "commit-msg", message_file(repo, message)).returncode == 0


def test_a_trailer_for_another_line_leaves_the_hint_open(repo: Path) -> None:
    stage_locator(repo)
    message = "Fetch from the application\n\nHint-Ack: service-locator cli/x.py:99 -- composition root of the daemon\n"
    done = run(repo, "commit-msg", message_file(repo, message))
    assert done.returncode == 1
    assert "matches no open site" in done.stderr


def test_a_malformed_trailer_is_reported(repo: Path) -> None:
    stage_locator(repo)
    done = run(
        repo, "commit-msg", message_file(repo, "x\n\nHint-Ack: service-locator cli/x.py:2\n")
    )
    assert done.returncode == 1
    assert "this line is not an answer" in done.stderr


def test_a_commit_without_hints_passes(repo: Path) -> None:
    (repo / "cli" / "x.py").write_text("VALUE = 2\n", encoding="utf-8")
    git(repo, "add", "cli/x.py")
    assert run(repo, "commit-msg", message_file(repo, "Change a value\n")).returncode == 0


def test_the_branch_check_accepts_an_answer_in_any_commit_of_the_branch(repo: Path) -> None:
    git(repo, "switch", "-q", "-c", "work")
    (repo / "cli" / "x.py").write_text(LOCATOR, encoding="utf-8")
    commit(repo, "Fetch from the application")
    assert run(repo, "branch").returncode == 1
    (repo / "cli" / "y.py").write_text("VALUE = 3\n", encoding="utf-8")
    commit(
        repo,
        "Another change\n\nHint-Ack: service-locator cli/x.py:2 -- composition root of the daemon",
    )
    assert run(repo, "branch").returncode == 0


def test_the_branch_check_judges_the_final_diff_not_each_commit(repo: Path) -> None:
    git(repo, "switch", "-q", "-c", "work")
    (repo / "cli" / "x.py").write_text(LOCATOR, encoding="utf-8")
    commit(repo, "Fetch from the application")
    (repo / "cli" / "x.py").write_text("VALUE = 1\n", encoding="utf-8")
    commit(repo, "Take it back out")
    assert run(repo, "branch").returncode == 0


def test_the_branch_check_needs_origin_main(repo: Path) -> None:
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    done = run(repo, "branch")
    assert done.returncode == 2
    assert "origin/main" in done.stderr


def test_report_lists_every_site_without_asking_for_answers(repo: Path) -> None:
    git(repo, "switch", "-q", "-c", "work")
    (repo / "cli" / "x.py").write_text(LOCATOR, encoding="utf-8")
    commit(repo, "Fetch from the application")
    done = run(repo, "report", "main", "HEAD")
    assert done.returncode == 0
    assert done.stdout.startswith("cli/x.py:2: hint service-locator")
    as_json = run(repo, "report", "main", "HEAD", "--json")
    assert '"hint": "service-locator"' in as_json.stdout


def test_a_mnemonic_prefix_config_does_not_blind_the_hook(repo: Path) -> None:
    git(repo, "config", "diff.mnemonicPrefix", "true")
    stage_locator(repo)
    done = run(repo, "commit-msg", message_file(repo, "Fetch from the application\n"))
    assert done.returncode == 1
    assert "cli/x.py:2: hint service-locator" in done.stderr


def test_a_no_prefix_config_does_not_blind_the_hook(repo: Path) -> None:
    git(repo, "config", "diff.noprefix", "true")
    stage_locator(repo)
    assert (
        run(repo, "commit-msg", message_file(repo, "Fetch from the application\n")).returncode == 1
    )


def test_a_pure_rename_asks_nothing(repo: Path) -> None:
    (repo / "cli" / "tests").mkdir()
    spy = "def test_a(m):\n    m.assert_called_once_with(1)\n"
    (repo / "cli" / "x.py").write_text(LOCATOR, encoding="utf-8")
    (repo / "cli" / "tests" / "test_a.py").write_text(spy, encoding="utf-8")
    commit(repo, "Add both")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "mv", "cli/x.py", "cli/y.py")
    git(repo, "mv", "cli/tests/test_a.py", "cli/tests/test_b.py")
    assert run(repo, "commit-msg", message_file(repo, "Move both\n")).returncode == 0


def test_a_rename_with_edits_asks_about_the_edits_only(repo: Path) -> None:
    filler = "".join(f"VALUE_{n} = {n}\n" for n in range(20))
    base = LOCATOR + filler
    (repo / "cli" / "x.py").write_text(base, encoding="utf-8")
    commit(repo, "Add the locator")
    git(repo, "mv", "cli/x.py", "cli/y.py")
    extended = base + "\n\ndef later(request):\n    return request.app.state.other\n"
    (repo / "cli" / "y.py").write_text(extended, encoding="utf-8")
    git(repo, "add", "cli/y.py")
    done = run(repo, "commit-msg", message_file(repo, "Move and extend\n"))
    assert done.returncode == 1
    added = len(extended.splitlines())
    assert f"cli/y.py:{added}: hint service-locator: `request.app.state`" in done.stderr
    assert "cli/y.py:2:" not in done.stderr


def test_the_branch_check_takes_an_explicit_base_and_writes_the_summary(
    repo: Path, tmp_path: Path
) -> None:
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-q", "-c", "work")
    (repo / "cli" / "x.py").write_text(LOCATOR, encoding="utf-8")
    commit(
        repo,
        "Fetch from the application\n\nHint-Ack: service-locator cli/ -- the whole package is a shell layer",
    )
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    summary = tmp_path / "summary.md"
    done = run(repo, "branch", "--base", base, "--summary", str(summary))
    assert done.returncode == 0, done.stderr
    text = summary.read_text(encoding="utf-8")
    assert "1 answer(s) on this branch; 0 site(s) still open." in text
    assert "`cli/` **broad**" in text
    assert "the whole package is a shell layer" in text


def test_a_narrative_line_that_starts_with_the_key_does_not_wedge_the_push(repo: Path) -> None:
    git(repo, "switch", "-q", "-c", "work")
    (repo / "cli" / "x.py").write_text("VALUE = 2\n", encoding="utf-8")
    commit(
        repo,
        "Change a value\n\nHint-Ack: is the name of the trailer this change\ndoes not use.\n\nSigned-off-by: T <t@meiva>",
    )
    done = run(repo, "branch")
    assert done.returncode == 0
    assert "outside the trailer block" in done.stderr


def test_the_commit_hint_reads_roots_from_the_index(repo: Path) -> None:
    (repo / "cli" / "x.py").write_text(LOCATOR, encoding="utf-8")
    manifest = repo / "cli" / "meizel.toml"
    manifest.write_text(ROOT_TABLE, encoding="utf-8")
    git(repo, "add", "cli/x.py", "cli/meizel.toml")
    manifest.write_text("", encoding="utf-8")
    message = (
        "Declare the process entry\n\nHint-Ack: root-middle-layer cli/x.py -- the daemon's entry\n"
    )
    done = run(repo, "commit-msg", message_file(repo, message))
    assert done.returncode == 0, done.stderr


def test_an_unstaged_root_does_not_answer_a_staged_read(repo: Path) -> None:
    (repo / "cli" / "x.py").write_text(LOCATOR, encoding="utf-8")
    manifest = repo / "cli" / "meizel.toml"
    manifest.write_text("", encoding="utf-8")
    git(repo, "add", "cli/x.py", "cli/meizel.toml")
    manifest.write_text(ROOT_TABLE, encoding="utf-8")
    done = run(repo, "commit-msg", message_file(repo, "Fetch application inputs\n"))
    assert done.returncode == 1
    assert "hint service-locator" in done.stderr


def test_the_branch_hint_uses_roots_at_the_head_revision(repo: Path) -> None:
    (repo / "cli" / "x.py").write_text(LOCATOR, encoding="utf-8")
    manifest = repo / "cli" / "meizel.toml"
    manifest.write_text(ROOT_TABLE, encoding="utf-8")
    commit(
        repo,
        "Declare the process entry\n\nHint-Ack: root-middle-layer cli/x.py -- the daemon's entry",
    )
    manifest.write_text("", encoding="utf-8")
    done = run(repo, "branch")
    assert done.returncode == 0, done.stderr


def test_an_unknown_root_kind_fails_the_hint_explicitly(repo: Path) -> None:
    stage_locator(repo)
    manifest = repo / "cli" / "meizel.toml"
    manifest.write_text(
        '[roots]\n"x.py" = { kinds = ["typo"], why = "process entry" }\n', encoding="utf-8"
    )
    git(repo, "add", "cli/meizel.toml")
    done = run(repo, "commit-msg", message_file(repo, "Invalid root\n"))
    assert done.returncode == 2
    assert "typo" in done.stderr


def test_a_list_of_root_kinds_fails_the_hint_explicitly(repo: Path) -> None:
    stage_locator(repo)
    manifest = repo / "cli" / "meizel.toml"
    manifest.write_text('[roots]\n"x.py" = ["settings"]\n', encoding="utf-8")
    git(repo, "add", "cli/meizel.toml")
    done = run(repo, "commit-msg", message_file(repo, "A bare list of kinds\n"))
    assert done.returncode == 2
    assert "must be a table with kinds and why" in done.stderr


# meiva:note
# A root `cli/home.py` imported by `importers` modules, some of which are roots themselves.
def declare_root(repo: Path, importers: int, *, as_roots: tuple[str, ...] = ()) -> None:
    (repo / "cli" / "home.py").write_text("VALUE = 1\n", encoding="utf-8")
    table = '[roots]\n"home.py" = { kinds = ["home"], why = "reads the home" }\n'
    for name in as_roots:
        table += f'"{name}.py" = {{ kinds = ["home"], why = "an entry" }}\n'
    (repo / "cli" / "meizel.toml").write_text(table, encoding="utf-8")
    for index in range(importers):
        (repo / "cli" / f"user{index}.py").write_text(
            "from cli.home import VALUE\n", encoding="utf-8"
        )
    commit(repo, "A root and its importers")
    (repo / "cli" / "home.py").write_text("VALUE = 2\n", encoding="utf-8")
    git(repo, "add", "cli/home.py")


def test_a_root_imported_by_two_modules_that_are_not_roots_is_asked_about(repo: Path) -> None:
    declare_root(repo, 2)
    done = run(repo, "commit-msg", message_file(repo, "Change the root\n"))
    assert done.returncode == 1
    assert "cli/home.py:1: hint root-middle-layer" in done.stderr
    assert (
        "imported by 2 production modules that are not roots: `cli/user0.py`, `cli/user1.py`"
        in done.stderr
    )


def test_a_root_with_fewer_importers_or_importers_that_are_roots_is_not(repo: Path) -> None:
    declare_root(repo, 3, as_roots=("user0", "user1"))
    assert run(repo, "commit-msg", message_file(repo, "Change the root\n")).returncode == 0


def test_the_answer_to_the_middle_layer_question_is_a_trailer(repo: Path) -> None:
    declare_root(repo, 4)
    message = "Change the root\n\nHint-Ack: root-middle-layer cli/home.py -- the process wiring, built once\n"
    assert run(repo, "commit-msg", message_file(repo, message)).returncode == 0


# meiva:note
# A root at `root` whose importers are the given files, committed with the root declared; then the
# root is edited so that the report of the last commit asks about it.
def build_root(repo: Path, root: str, importers: dict[str, str], *, kinds: str = '"home"') -> None:
    root_path = repo / root
    root_path.parent.mkdir(parents=True, exist_ok=True)
    root_path.write_text("VALUE = 1\n", encoding="utf-8")
    manifest = root_path.parent / "meizel.toml"
    manifest.write_text(
        f'[roots]\n"{root_path.name}" = {{ kinds = [{kinds}], why = "reads the home" }}\n',
        encoding="utf-8",
    )
    for name, text in importers.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    commit(repo, "A root and its importers")
    root_path.write_text("VALUE = 2\n", encoding="utf-8")
    commit(repo, "Change the root")


def reported(repo: Path) -> str:
    done = run(repo, "report", "HEAD~1", "HEAD")
    assert done.returncode == 0, done.stderr
    return done.stdout


IMPORT_FORMS = {
    "absolute": ("cli/home.py", "from cli.home import VALUE\n"),
    "plain": ("cli/home.py", "import cli.home\n"),
    "package": ("cli/home.py", "from cli import home\n"),
    "beside": ("cli/home.py", "from .home import VALUE\n"),
    "beside module": ("cli/home.py", "from . import home\n"),
    "parent to child": ("cli/sub/home.py", "from .sub.home import VALUE\n"),
    "child to parent": ("cli/home.py", "from ..home import VALUE\n"),
}


@pytest.mark.parametrize("form", sorted(IMPORT_FORMS))
def test_every_form_of_import_counts_an_importer(repo: Path, form: str) -> None:
    root, text = IMPORT_FORMS[form]
    where = {"parent to child": "cli", "child to parent": "cli/sub"}.get(form, "cli")
    build_root(repo, root, {f"{where}/user0.py": text, f"{where}/user1.py": text})
    assert "imported by 2 production modules" in reported(repo)


def test_text_that_looks_like_an_import_and_a_test_are_not_importers(repo: Path) -> None:
    quoted = 'TEMPLATE = """\nfrom cli.home import VALUE\n"""\n'
    build_root(
        repo,
        "cli/home.py",
        {
            "cli/user0.py": "from cli.home import VALUE\n",
            "cli/quoted.py": quoted,
            "cli/tests/test_home.py": "from cli.home import VALUE\n",
        },
    )
    assert reported(repo) == ""


@pytest.mark.parametrize("name", ["main.py", "__main__.py", "runtime.py", "app.py"])
def test_a_file_with_an_entry_name_is_not_asked(repo: Path, name: str) -> None:
    build_root(
        repo,
        f"cli/{name}",
        {"cli/user0.py": f"from cli.{name[:-3]} import VALUE\n"}
        | {"cli/user1.py": f"from cli.{name[:-3]} import VALUE\n"},
    )
    assert reported(repo) == ""


def test_a_root_with_one_importer_that_many_modules_import_is_asked_about(repo: Path) -> None:
    importers = {"cli/api_base.py": "from cli.public_env import VALUE\nAPI_BASE = VALUE\n"}
    importers |= {f"cli/use{index}.py": "from cli.api_base import API_BASE\n" for index in range(3)}
    build_root(repo, "cli/public_env.py", importers)
    assert "imported by `cli/api_base.py`, which 3 production modules" in reported(repo)


def test_a_root_declared_by_the_manifest_alone_is_asked_about(repo: Path) -> None:
    (repo / "cli" / "home.py").write_text("VALUE = 1\n", encoding="utf-8")
    for index in range(5):
        (repo / "cli" / f"user{index}.py").write_text(
            "from cli.home import VALUE\n", encoding="utf-8"
        )
    commit(repo, "A module and its importers")
    (repo / "cli" / "meizel.toml").write_text(
        '[roots]\n"home.py" = { kinds = ["home"], why = "reads the home" }\n', encoding="utf-8"
    )
    git(repo, "add", "cli/meizel.toml")
    done = run(repo, "commit-msg", message_file(repo, "Declare the home reader\n"))
    assert done.returncode == 1
    assert (
        "cli/home.py:1: hint root-middle-layer: declares a root (home) in cli/meizel.toml"
        in done.stderr
    )
    assert "5 production modules that are not roots import it" in done.stderr
    message = "Declare the home reader\n\nHint-Ack: root-middle-layer cli/home.py -- built once by the daemon\n"
    assert run(repo, "commit-msg", message_file(repo, message)).returncode == 0


def test_a_typescript_root_declared_by_the_manifest_is_asked_about(repo: Path) -> None:
    (repo / "ui" / "web" / "src").mkdir(parents=True)
    (repo / "ui" / "web" / "src" / "env.ts").write_text("export const API = ''\n", encoding="utf-8")
    (repo / "ui" / "web" / "meizel.toml").write_text(
        '[roots]\n"src/env.ts" = { kinds = ["environ"], why = "reads the public env" }\n',
        encoding="utf-8",
    )
    git(repo, "add", "ui/web")
    done = run(repo, "commit-msg", message_file(repo, "Declare the public env\n"))
    assert done.returncode == 1
    assert "ui/web/src/env.ts:1: hint root-middle-layer" in done.stderr
    assert "importers are not read for this language" in done.stderr


def test_a_root_that_was_already_declared_is_not_asked_when_the_manifest_changes(
    repo: Path,
) -> None:
    build_root(repo, "cli/home.py", {})
    manifest = repo / "cli" / "meizel.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("reads the home", "reads it"), encoding="utf-8"
    )
    commit(repo, "Reword the reason")
    assert reported(repo) == ""


def promote(repo: Path, shell_text: str) -> None:
    (repo / "cli" / "plan.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "cli" / "shell.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "cli" / "meizel.toml").write_text(
        '[purity]\n"plan.py" = "strict-to-be"\n', encoding="utf-8"
    )
    commit(repo, "A plan and its shell")
    (repo / "cli" / "meizel.toml").write_text('[purity]\n"plan.py" = "strict"\n', encoding="utf-8")
    (repo / "cli" / "shell.py").write_text(shell_text, encoding="utf-8")
    git(repo, "add", "cli")


def test_a_promotion_that_adds_a_read_beside_it_is_asked_about_and_answered(repo: Path) -> None:
    promote(repo, "import os\n\ndef run(path):\n    return os.stat(path)\n")
    done = run(repo, "commit-msg", message_file(repo, "Promote the plan\n"))
    assert done.returncode == 1
    assert (
        "cli/plan.py:1: hint strict-promotion: promoted to strict (was strict-to-be)" in done.stderr
    )
    assert "`cli/shell.py: return os.stat(path)`" in done.stderr
    message = (
        "Promote the plan\n\nHint-Ack: strict-promotion cli/plan.py -- shell.run runs the plan\n"
    )
    assert run(repo, "commit-msg", message_file(repo, message)).returncode == 0


def test_a_promotion_with_no_read_added_beside_it_is_not_asked_about(repo: Path) -> None:
    promote(repo, "VALUE = 2\n")
    assert run(repo, "commit-msg", message_file(repo, "Promote the plan\n")).returncode == 0


def test_the_statistics_command_counts_and_never_asks(repo: Path) -> None:
    (repo / "cli" / "tests").mkdir()
    (repo / "cli" / "tests" / "test_work.py").write_text(
        "from cli.work import run\n\nrun(1, grace=2)\n", encoding="utf-8"
    )
    (repo / "cli" / "work.py").write_text(
        "from base.clock import Clock\n\ndef run(config, grace=5):\n    return Clock(config)\n",
        encoding="utf-8",
    )
    commit(repo, "A function with an optional parameter only a test passes")
    done = run(repo, "stats", "HEAD~1", "HEAD", "--sites", "5")
    assert done.returncode == 0, done.stderr
    assert "optional-test-only: 1 sites in 1 files" in done.stdout
    assert "clock-in-callee: 1 sites in 1 files" in done.stdout
    assert (
        "cli/work.py:3: optional-test-only: `grace` of `run` is passed by keyword in 1 test"
        in done.stdout
    )
    tree = run(repo, "stats", "--tree", "HEAD")
    assert tree.returncode == 0, tree.stderr
    assert "clock-in-callee: 1 sites in 1 files" in tree.stdout
