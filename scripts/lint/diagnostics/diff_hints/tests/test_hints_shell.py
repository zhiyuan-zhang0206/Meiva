from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

HINTS = Path(__file__).resolve().parents[1] / "hints.py"
LOCATOR = "def inputs(request):\n    return request.app.state.thing\n"


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


@pytest.mark.parametrize(
    "table",
    [
        '[roots]\n"x.py" = ["settings"]\n',
        '[roots]\n"x.py" = { kinds = ["settings"], why = "process entry" }\n',
    ],
)
def test_the_commit_hint_reads_roots_from_the_index(repo: Path, table: str) -> None:
    (repo / "cli" / "x.py").write_text(LOCATOR, encoding="utf-8")
    manifest = repo / "cli" / "meizel.toml"
    manifest.write_text(table, encoding="utf-8")
    git(repo, "add", "cli/x.py", "cli/meizel.toml")
    manifest.write_text("", encoding="utf-8")
    done = run(repo, "commit-msg", message_file(repo, "Declare the process entry\n"))
    assert done.returncode == 0, done.stderr


def test_an_unstaged_root_does_not_answer_a_staged_read(repo: Path) -> None:
    (repo / "cli" / "x.py").write_text(LOCATOR, encoding="utf-8")
    manifest = repo / "cli" / "meizel.toml"
    manifest.write_text("", encoding="utf-8")
    git(repo, "add", "cli/x.py", "cli/meizel.toml")
    manifest.write_text('[roots]\n"x.py" = ["settings"]\n', encoding="utf-8")
    done = run(repo, "commit-msg", message_file(repo, "Fetch application inputs\n"))
    assert done.returncode == 1
    assert "hint service-locator" in done.stderr


def test_the_branch_hint_uses_roots_at_the_head_revision(repo: Path) -> None:
    (repo / "cli" / "x.py").write_text(LOCATOR, encoding="utf-8")
    manifest = repo / "cli" / "meizel.toml"
    manifest.write_text('[roots]\n"x.py" = ["settings"]\n', encoding="utf-8")
    commit(repo, "Declare the process entry")
    manifest.write_text("", encoding="utf-8")
    done = run(repo, "branch")
    assert done.returncode == 0, done.stderr


def test_an_unknown_root_kind_fails_the_hint_explicitly(repo: Path) -> None:
    stage_locator(repo)
    manifest = repo / "cli" / "meizel.toml"
    manifest.write_text('[roots]\n"x.py" = ["typo"]\n', encoding="utf-8")
    git(repo, "add", "cli/meizel.toml")
    done = run(repo, "commit-msg", message_file(repo, "Invalid root\n"))
    assert done.returncode == 2
    assert "typo" in done.stderr
