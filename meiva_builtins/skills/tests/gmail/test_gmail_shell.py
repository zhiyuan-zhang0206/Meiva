# meiva:note
# Tests of the gmail skill's shells, against real resources.
#
# The mailbox itself (IMAP/SMTP against Google) never runs here. What can run for real does: the
# Keychain read runs a stand-in `security` program written under tmp_path, the secrets file is a
# real file, the raw mirror writes under tmp_path, and the CLI runs as a child process. What the
# shells decide is tested as values in test_gmail_values.py.


from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from meiva_builtins.skills.tests.gmail.gmail_modules import load_gmail_modules

M = load_gmail_modules()
auth = M.auth
imap = M.imap
feed = M.feed

NOW_TS = 1_800_000_000
_FEED = Path(__file__).resolve().parents[2] / "integrations" / "gmail" / "scripts" / "feed.py"


def test_now_iso_ends_with_z() -> None:
    assert imap.now_iso().endswith("Z")


_SECURITY = """
import json
import sys
import time
from pathlib import Path

config = json.loads((Path(sys.argv[0]).parent / "security.json").read_text())
time.sleep(config["sleep"])
sys.stderr.write(config["stderr"])
if config["exit_code"]:
    sys.exit(config["exit_code"])
sys.stdout.write(config["password"] if "-w" in sys.argv[1:] else config["dump"])
"""

_DUMP = 'class: "genp"\nattributes:\n    "acct"<blob>="stored@example.com"\n'


@pytest.fixture(scope="module")
def security_script(tmp_path_factory: pytest.TempPathFactory) -> Path:
    script = tmp_path_factory.mktemp("security-stand-in") / "security.py"
    script.write_text(f"#!{sys.executable}\n{_SECURITY}")
    script.chmod(0o755)
    return script


# meiva:note
# A stand-in for the macOS `security` program, made once per module and linked under that
# name (a fresh executable costs a first-run scan on macOS). Its behavior is the JSON next
# to the link: the attribute dump or the password it prints, its exit code and stderr, and
# how long it hangs first.
class Keychain:
    def __init__(self, script: Path, root: Path) -> None:
        self.home = root / "home"
        self.bin_dir = root / "bin"
        self.home.mkdir()
        self.bin_dir.mkdir()
        (self.bin_dir / "security").symlink_to(script)

    def sources(
        self,
        *,
        account_env: str = "",
        file_text: str | None = None,
        dump: str = _DUMP,
        keychain_text: str = "",
        exit_code: int = 0,
        stderr: str = "",
        sleep: float = 0.0,
        timeout_s: float = 15.0,
        program_present: bool = True,
    ) -> Any:
        config = {
            "dump": dump,
            "password": keychain_text,
            "exit_code": exit_code,
            "stderr": stderr,
            "sleep": sleep,
        }
        (self.bin_dir / "security.json").write_text(json.dumps(config))
        password_file = self.home / ".meiva" / "secrets" / "gmail-app-password"
        if file_text is not None:
            password_file.parent.mkdir(parents=True)
            password_file.write_text(file_text)
        return auth.AuthSources(
            account_env=account_env,
            password_file=password_file,
            security_cmd=str(self.bin_dir / ("security" if program_present else "absent")),
            keychain_timeout_s=timeout_s,
        )


@pytest.fixture
def keychain(tmp_path: Path, security_script: Path) -> Keychain:
    return Keychain(security_script, tmp_path)


# meiva:note
# The secrets file (read before the Keychain) is deliberately machine-level, like the Keychain
# entry it substitutes for: it is the path in the sources, never one derived from a cluster
# home. The Keychain here would fail, so the file has to answer.
def test_app_password_comes_from_the_machine_level_file(keychain: Keychain) -> None:
    sources = keychain.sources(file_text="abcd efgh", program_present=False)
    assert imap.app_password(sources) == "abcdefgh"


# meiva:note
# When the file and the Keychain both hold a password, the file wins (user ruling 2026-08-26:
# automation must not depend on the Keychain).
def test_app_password_prefers_file_over_keychain(keychain: Keychain) -> None:
    sources = keychain.sources(file_text="file-pass", keychain_text="keychain-pass")
    assert imap.app_password(sources) == "file-pass"


def test_app_password_falls_back_to_the_keychain_without_a_file(keychain: Keychain) -> None:
    sources = keychain.sources(keychain_text="abcd efgh ijkl\n")
    assert imap.app_password(sources) == "abcdefghijkl"


@pytest.mark.parametrize("file_text", ["", "  \n"])
def test_a_blank_secrets_file_falls_through_to_the_keychain(
    keychain: Keychain, file_text: str
) -> None:
    sources = keychain.sources(file_text=file_text, keychain_text="keychain-pass")
    assert imap.app_password(sources) == "keychain-pass"


def test_an_unreadable_secrets_file_falls_through_to_the_keychain(keychain: Keychain) -> None:
    sources = keychain.sources(keychain_text="keychain-pass")
    sources.password_file.mkdir(parents=True)
    assert imap.app_password(sources) == "keychain-pass"


def test_no_password_source_raises_one_error_naming_both(keychain: Keychain) -> None:
    sources = keychain.sources(exit_code=44, stderr="item not found")
    with pytest.raises(auth.GmailError, match="no Gmail app password") as exc:
        imap.app_password(sources)
    assert str(sources.password_file) in str(exc.value)
    assert "meiva-gmail-imap" in str(exc.value)


def test_a_blank_keychain_secret_counts_as_missing(keychain: Keychain) -> None:
    with pytest.raises(auth.GmailError, match="no Gmail app password"):
        imap.app_password(keychain.sources(keychain_text="  \n"))


# meiva:note
# GMAIL_ACCOUNT supplies the login without touching the Keychain (which is absent here).
def test_account_env_override(keychain: Keychain) -> None:
    sources = keychain.sources(account_env="env@example.com", program_present=False)
    assert imap.account(sources) == "env@example.com"


def test_account_is_the_keychain_label_without_an_override(keychain: Keychain) -> None:
    assert imap.account(keychain.sources()) == "stored@example.com"


def test_keychain_read_returns_the_selected_field(keychain: Keychain) -> None:
    sources = keychain.sources(keychain_text="secret")
    assert imap.keychain_read(sources) == _DUMP
    assert imap.keychain_read(sources, "-w") == "secret"


# meiva:note
# A hanging `security` call becomes a GmailError after the cap, not an unhandled TimeoutExpired.
def test_keychain_timeout_is_a_clean_error(keychain: Keychain) -> None:
    sources = keychain.sources(sleep=5.0, timeout_s=0.3)
    with pytest.raises(auth.GmailError, match=r"timed out after 0\.3s"):
        imap.keychain_read(sources)


def test_keychain_refusal_carries_the_programs_stderr(keychain: Keychain) -> None:
    sources = keychain.sources(exit_code=44, stderr="The specified item could not be found\n")
    with pytest.raises(auth.GmailError, match="specified item could not be found"):
        imap.keychain_read(sources)


def test_a_missing_security_program_is_a_clean_error(keychain: Keychain) -> None:
    with pytest.raises(auth.GmailError, match="security command not found"):
        imap.keychain_read(keychain.sources(program_present=False))


# meiva:note
# Both secrets are resolved before the socket opens: with none configured the failure is the
# credential error, not a network one.
def test_a_missing_credential_fails_before_any_connection(keychain: Keychain) -> None:
    sources = keychain.sources(account_env="me@example.com", program_present=False)
    with pytest.raises(auth.GmailError, match="no Gmail app password"):
        imap.connection(sources)


def _post(message_id: str = "abc@example.com") -> dict[str, Any]:
    return {
        "platform": "gmail",
        "message_id": message_id,
        "list_id": "news.example.com",
        "url": "https://example.com/p/1",
        "title": "Issue 1",
        "text": "Body text",
        "author": {"id": "news.example.com", "name": "News"},
        "published_at": "2026-06-21T15:30:00Z",
        "attachments": [
            {"content_type": "image/png", "path": "/m/a.png"},
            {"content_type": "application/pdf", "path": "/m/a.pdf"},
        ],
        "fetched_at": "2026-06-22T00:00:00Z",
        "fetched_via": "gmail",
    }


def test_save_writes_the_raw_message_the_post_and_a_markdown_rendering(tmp_path: Path) -> None:
    outdir = imap.save(_post(), b"raw bytes", root=tmp_path)
    assert outdir == tmp_path / "abc_example.com"
    assert (outdir / "raw.eml").read_bytes() == b"raw bytes"
    assert json.loads((outdir / "post.json").read_text())["title"] == "Issue 1"
    assert (outdir / "post.md").read_text() == (
        "# Issue 1\n\n- **from**: News (list-id news.example.com)\n"
        "- **url**: https://example.com/p/1\n- **published**: 2026-06-21T15:30:00Z\n\nBody text\n"
    )


def test_save_refuses_a_message_without_an_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no Message-Id"):
        imap.save({**_post(), "message_id": ""}, b"raw", root=tmp_path)


@pytest.mark.parametrize("hostile", ["..", ".", "../../etc/passwd", "a/b", ""])
def test_a_sender_chosen_message_id_cannot_leave_the_mirror(tmp_path: Path, hostile: str) -> None:
    root = tmp_path / "mirror"
    root.mkdir()
    outdir = imap.save({**_post(), "message_id": hostile or "<>"}, b"raw", root=root)
    assert outdir.resolve().is_relative_to(root.resolve())
    assert outdir.parent == root


def test_a_very_long_message_id_gets_a_bounded_directory_name(tmp_path: Path) -> None:
    outdir = imap.save({**_post(), "message_id": "x" * 500}, b"raw", root=tmp_path)
    assert len(outdir.name) < 120


def test_to_s1_points_raw_path_at_the_root_the_message_was_saved_under(tmp_path: Path) -> None:
    item = imap.to_s1(_post(), root=tmp_path)
    assert item["raw_path"] == f"{tmp_path / 'abc_example.com'}/"
    assert item["source_id"] == "abc@example.com"
    assert item["media"] == [{"kind": "image", "path": "/m/a.png"}]
    assert item["author"] == {"id": "news.example.com", "name": "News"}
    assert item["matrix_entity"] is None
    assert imap.to_s1(_post(), root=tmp_path, matrix_entity="e1")["matrix_entity"] == "e1"


def _process(tmp_path: Path) -> Any:
    sources = auth.AuthSources(
        account_env="me@example.com",
        password_file=tmp_path / "no-password-file",
        security_cmd=str(tmp_path / "no-security"),
        keychain_timeout_s=1.0,
    )
    return feed.Process(auth=sources, mirror_root=tmp_path / "mirror", now_ts=NOW_TS)


def test_main_send_dry_run_prints_the_composed_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["send", "--to", "test@example.com", "--subject", "Test", "--body", "Hello world"]
    feed.main([*argv, "--dry-run"], _process(tmp_path), io.StringIO())
    data = json.loads(capsys.readouterr().out)
    assert data["sent"] is False
    assert data["subject"] == "Test"
    assert data["to"] == "test@example.com"
    assert data["from"] == "me@example.com"
    assert "Hello world" in data["body"]


def test_main_send_reads_the_body_from_stdin_when_omitted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["send", "--to", "a@b.com", "--subject", "S", "--dry-run"]
    feed.main(argv, _process(tmp_path), io.StringIO("piped body\n"))
    assert json.loads(capsys.readouterr().out)["body"] == "piped body"


def test_main_send_refuses_an_empty_piped_body(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no body"):
        feed.main(
            ["send", "--to", "a@b.com", "--subject", "S", "--dry-run"],
            _process(tmp_path),
            io.StringIO(),
        )


def test_main_send_carries_attachments_and_refuses_a_missing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    attachment = tmp_path / "report.pdf"
    attachment.write_bytes(b"%PDF-1.4")
    argv = ["send", "--to", "a@b.com", "--subject", "S", "--body", "see attached", "--dry-run"]
    feed.main([*argv, "--attach", str(attachment)], _process(tmp_path), io.StringIO())
    assert json.loads(capsys.readouterr().out)["attachments"] == ["report.pdf"]
    with pytest.raises(ValueError, match="not a readable file"):
        feed.main(
            [*argv, "--attach", str(tmp_path / "missing.pdf")], _process(tmp_path), io.StringIO()
        )


def test_main_draft_dry_run_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["draft", "--to", "a@b.com", "--subject", "S", "--body", "b", "--dry-run"]
    feed.main(argv, _process(tmp_path), io.StringIO())
    data = json.loads(capsys.readouterr().out)
    assert data["drafted"] is False and data["subject"] == "S"


def test_a_real_send_without_a_password_fails_before_any_smtp_connection(
    tmp_path: Path,
) -> None:
    with pytest.raises(auth.GmailError, match="no Gmail app password"):
        M.smtp.send(_process(tmp_path).auth, "a@b.com", "Subject", "Body")


# meiva:note
# reply --dry-run needs the mailbox, hence the app password; with no source for it the failure
# is the credential error rather than a traceback from the network layer.
def test_main_reply_without_a_password_fails_cleanly_before_any_connection(
    tmp_path: Path,
) -> None:
    argv = ["reply", "--message-id", "<test@host>", "--body", "Thanks", "--dry-run"]
    with pytest.raises(auth.GmailError, match="no Gmail app password"):
        feed.main(argv, _process(tmp_path), io.StringIO())


def test_main_rejects_an_unreadable_since_window(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="--since must be like"):
        feed.main(
            ["enum", "--list-id", "x.example.com", "--since", "soon"],
            _process(tmp_path),
            io.StringIO(),
        )


def _run_cli(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    # meiva:note
    # The argv is this interpreter and the skill script inside the repository, never input.
    return subprocess.run(  # noqa: S603
        [sys.executable, str(_FEED), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={
            "HOME": str(home),
            "MEIVA_HOME": str(tmp_path / "cluster-home"),
            "GMAIL_ACCOUNT": "env@example.com",
            "PATH": str(tmp_path / "bin"),
        },
    )


# meiva:note
# --dry-run send prints JSON with sent=false, no SMTP connection and no Keychain.
def test_cli_send_dry_run(tmp_path: Path) -> None:
    result = _run_cli(
        tmp_path,
        "send",
        "--to",
        "test@example.com",
        "--subject",
        "Test",
        "--body",
        "Hello world",
        "--dry-run",
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["sent"] is False
    assert data["subject"] == "Test"
    assert data["to"] == "test@example.com"
    assert data["from"] == "env@example.com"
    assert "Hello world" in data["body"]


def test_cli_reply_dry_run_reports_the_missing_credential_not_a_traceback_from_the_network(
    tmp_path: Path,
) -> None:
    result = _run_cli(
        tmp_path, "reply", "--message-id", "<test@host>", "--body", "Thanks", "--dry-run"
    )
    assert result.returncode != 0
    assert "GmailError: no Gmail app password" in result.stderr
    assert "Traceback" not in result.stderr
    assert result.stdout == ""


# meiva:note
# The CLI entry looks for the secrets file under the HOME it runs with, like the Keychain
# entry it substitutes for, and never under the cluster home (MEIVA_HOME): the missing-file
# error names the path that was tried.
def test_cli_looks_for_the_password_file_under_home_not_the_cluster_home(tmp_path: Path) -> None:
    result = _run_cli(
        tmp_path, "reply", "--message-id", "<test@host>", "--body", "Thanks", "--dry-run"
    )
    tried = tmp_path / "home" / ".meiva" / "secrets" / "gmail-app-password"
    assert f"file {tried} unreadable/missing" in result.stderr
    assert str(tmp_path / "cluster-home") not in result.stderr


# meiva:note
# send without --to should fail with a clear error.
def test_cli_send_no_recipient_errors(tmp_path: Path) -> None:
    result = _run_cli(tmp_path, "send", "--subject", "Test", "--body", "Hello", "--dry-run")
    assert result.returncode != 0
    assert "--to" in result.stderr


# meiva:note
# The CLI prints help without importing or connecting.
def test_cli_help(tmp_path: Path) -> None:
    result = _run_cli(tmp_path, "--help")
    assert result.returncode == 0
    assert "search" in result.stdout
    assert "send" in result.stdout
    assert "reply" in result.stdout


@pytest.mark.parametrize(
    "argv, reason",
    [
        (
            ("send", "--to", "a@example.com", "--subject", "s", "--body", " ", "--dry-run"),
            "no body",
        ),
        (("enum", "--list-id", "x.example.com", "--since", "soon"), "--since must be like"),
        (
            ("send", "--to", " ", "--subject", "s", "--body", "b", "--dry-run"),
            "at least one recipient",
        ),
        (
            (
                "send",
                "--to",
                "a@example.com",
                "--subject",
                "s",
                "--body",
                "b",
                "--attach",
                "/missing-meiva-gmail-attachment",
                "--dry-run",
            ),
            "--attach path",
        ),
        (("search", "--query", "x", "--limit", "-1"), "limit/scan must be >= 0"),
        (
            ("forward", "--message-id", "<x@host>", "--to", " ", "--dry-run"),
            "forward needs at least one recipient",
        ),
    ],
)
def test_cli_input_errors_are_domain_errors_without_a_traceback(
    tmp_path: Path, argv: tuple[str, ...], reason: str
) -> None:
    result = _run_cli(tmp_path, *argv)
    assert result.returncode == 1
    assert result.stdout == ""
    assert "GmailInputError:" in result.stderr
    assert reason in result.stderr
    assert "Traceback" not in result.stderr
