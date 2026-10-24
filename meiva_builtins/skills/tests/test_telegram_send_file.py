# meiva:note
# Hermetic tests for the telegram-send-file skill script.
#
# The skill's live behavior (an outward POST to api.telegram.org) never runs here: real external
# sends require explicit user approval. The decisions are tested as values (the credential
# aliases, the request, how a response reads), and the shell runs against a real HTTP server on
# a loopback port that plays the Bot API: the multipart payload shape, the validation gates and
# the token-hygiene error paths (httpx errors must never surface the request URL, which carries
# the bot token).


from __future__ import annotations

import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from base.config import settings
from base.host.net.tests.local_http import closed_port_url
from meiva_builtins.skills.tests.local_post_server import (
    RecordedPost,
    Reply,
    json_reply,
    serve_posts,
    text_reply,
)
from meiva_builtins.skills.tests.skill_script import load_skill_script

delivery = load_skill_script(
    "integrations",
    "telegram-send-file",
    "scripts",
    "telegram_delivery.py",
    name="telegram_delivery",
)
send_file = load_skill_script(
    "integrations",
    "telegram-send-file",
    "scripts",
    "send_file.py",
    name="telegram_send_file_under_test",
)

# meiva:note
# A fixture value, never a real secret.
FAKE_BOT_TOKEN = "123456:TEST-TOKEN"  # noqa: S105
OWNER = 123456789
CREDENTIALS = delivery.Credentials(token=FAKE_BOT_TOKEN, chat_id=OWNER)
NO_CREDENTIALS = delivery.Credentials(token="", chat_id=0)


def _file(tmp_path: Path, name: str = "f.bin", content: bytes = b"data") -> Path:
    path = tmp_path / name
    path.write_bytes(content)
    return path


def _ok(message_id: int = 42) -> Reply:
    return json_reply({"ok": True, "result": {"message_id": message_id}})


def _send(api_base: str, path: Path, caption: str | None = None) -> Mapping[str, object]:
    return send_file.send_document(
        api_base=api_base, credentials=CREDENTIALS, path=path, caption=caption
    )


def _body(post: RecordedPost) -> str:
    return post.body.decode("latin-1")


def test_validate_file_accepts_regular_file(tmp_path: Path) -> None:
    p = _file(tmp_path, "report.pdf", b"x" * 100)
    assert send_file.validate_file(p) == p


def test_validate_file_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        send_file.validate_file(tmp_path / "nope.pdf")


def test_validate_file_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        send_file.validate_file(tmp_path)


def test_validate_file_empty_raises(tmp_path: Path) -> None:
    p = _file(tmp_path, "empty.txt", b"")
    with pytest.raises(ValueError, match="empty"):
        send_file.validate_file(p)


def test_validate_file_over_50mb_raises(tmp_path: Path) -> None:
    p = tmp_path / "huge.bin"
    with p.open("wb") as fh:
        fh.seek(delivery.MAX_DOCUMENT_BYTES + 1)
        fh.write(b"x")
    with pytest.raises(ValueError, match="50 MB"):
        send_file.validate_file(p)


def test_size_gate_accepts_the_cap_and_refuses_one_byte_over() -> None:
    delivery.check_document_size("f", delivery.MAX_DOCUMENT_BYTES)
    with pytest.raises(ValueError, match="exceeds"):
        delivery.check_document_size("f", delivery.MAX_DOCUMENT_BYTES + 1)


def test_build_request_names_the_bot_url_and_the_form() -> None:
    request = delivery.build_request("https://api.example.test", CREDENTIALS, "Weekly")
    assert request.url == f"https://api.example.test/bot{FAKE_BOT_TOKEN}/sendDocument"
    assert request.form() == {"chat_id": OWNER, "caption": "Weekly"}


def test_build_request_caption_optional() -> None:
    request = delivery.build_request("https://api.example.test", CREDENTIALS, None)
    assert request.form() == {"chat_id": OWNER}


def test_build_request_caption_too_long_raises() -> None:
    with pytest.raises(ValueError, match="1024"):
        delivery.build_request(
            "https://api.example.test", CREDENTIALS, "x" * (delivery.MAX_CAPTION_CHARS + 1)
        )


def test_caption_at_the_cap_is_accepted() -> None:
    delivery.check_caption("x" * delivery.MAX_CAPTION_CHARS)
    delivery.check_caption(None)


def test_read_response_returns_the_result_object() -> None:
    body = '{"ok": true, "result": {"message_id": 42, "chat": {"id": 1}}}'
    assert delivery.read_response(200, body) == {"message_id": 42, "chat": {"id": 1}}


def test_read_response_http_error_keeps_status_and_a_body_prefix() -> None:
    with pytest.raises(RuntimeError, match="HTTP 400 - Bad Request") as exc:
        delivery.read_response(400, "Bad Request: file is too big" + "!" * 500)
    assert len(str(exc.value)) < 300


def test_read_response_api_error_description_reported() -> None:
    with pytest.raises(RuntimeError, match="chat not found"):
        delivery.read_response(200, '{"ok": false, "description": "chat not found"}')
    with pytest.raises(RuntimeError, match="unknown error"):
        delivery.read_response(200, '{"ok": false}')


@pytest.mark.parametrize(
    "body",
    ["[]", '"ok"', '{"ok": true}', '{"ok": true, "result": []}', '{"ok": true, "result": {}}'],
)
def test_read_response_refuses_a_body_without_a_message_id(body: str) -> None:
    with pytest.raises(RuntimeError, match=r"non-object body|no message_id"):
        delivery.read_response(200, body)


def test_success_line() -> None:
    assert delivery.success_line("report.pdf", 12345, OWNER, {"message_id": 42}) == (
        f"sent report.pdf (12345 bytes) to chat {OWNER}: message_id=42"
    )


def test_credentials_from_env_reads_aliases() -> None:
    env = {"MEIVA_TELEGRAM_BOT_TOKEN": "t", "MEIVA_TELEGRAM_OWNER_ID": "777"}
    assert delivery.credentials_from_env(env) == delivery.Credentials("t", 777)


def test_credentials_from_env_malformed_owner_reads_zero() -> None:
    env = {"MEIVA_TELEGRAM_BOT_TOKEN": "t", "MEIVA_TELEGRAM_OWNER_ID": "not-a-number"}
    assert delivery.credentials_from_env(env) == delivery.Credentials("t", 0)


def test_credentials_from_env_missing_reads_empty() -> None:
    assert delivery.credentials_from_env({}) == NO_CREDENTIALS


def test_unconfigured_credentials_name_the_missing_alias() -> None:
    with pytest.raises(RuntimeError, match="MEIVA_TELEGRAM_BOT_TOKEN"):
        NO_CREDENTIALS.require()
    with pytest.raises(RuntimeError, match="MEIVA_TELEGRAM_OWNER_ID"):
        delivery.Credentials("t", 0).require()
    CREDENTIALS.require()


def test_read_credentials_prefers_env_over_settings() -> None:
    env = {"MEIVA_TELEGRAM_BOT_TOKEN": "env:TOKEN", "MEIVA_TELEGRAM_OWNER_ID": "777"}
    assert send_file.read_credentials(env) == delivery.Credentials("env:TOKEN", 777)


def test_read_credentials_falls_back_to_the_settings_domain_when_env_is_empty() -> None:
    assert send_file.read_credentials({"MEIVA_TELEGRAM_BOT_TOKEN": ""}) == delivery.Credentials(
        token=settings.telegram.telegram_bot_token, chat_id=settings.telegram.telegram_owner_id
    )


def _run_script(home: Path, file: Path) -> subprocess.CompletedProcess[str]:
    env = {"HOME": str(home), "MEIVA_HOME": str(home), "PATH": "/usr/bin:/bin"}
    # meiva:note
    # The argv is this interpreter and the skill script inside the repository, never input.
    return subprocess.run(  # noqa: S603
        [sys.executable, str(send_file.__file__), str(file)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=env,
    )


def test_script_reads_the_token_from_the_cluster_env_file(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("MEIVA_TELEGRAM_BOT_TOKEN=123:from-dotenv\n")
    result = _run_script(home, _file(tmp_path))
    assert result.returncode == 1
    assert "MEIVA_TELEGRAM_OWNER_ID" in result.stderr
    assert "from-dotenv" not in result.stderr + result.stdout


def test_send_document_posts_multipart_to_senddocument(tmp_path: Path) -> None:
    p = _file(tmp_path, "report.txt", b"hello telegram")
    with serve_posts([_ok()]) as server:
        result = _send(server.url, p, "Weekly")

    assert result["message_id"] == 42
    [post] = server.requests
    assert post.path == f"/bot{FAKE_BOT_TOKEN}/sendDocument"
    assert post.headers["content-type"].startswith("multipart/form-data")
    body = _body(post)
    assert 'name="chat_id"' in body and str(OWNER) in body
    assert 'name="caption"' in body and "Weekly" in body
    assert 'name="document"' in body and 'filename="report.txt"' in body
    assert b"hello telegram" in post.body


def test_send_document_guesses_the_content_type_from_the_name(tmp_path: Path) -> None:
    with serve_posts([_ok(), _ok()]) as server:
        _send(server.url, _file(tmp_path, "report.pdf"))
        _send(server.url, _file(tmp_path, "blob.unknownext"))
    pdf, unknown = (_body(post) for post in server.requests)
    assert "Content-Type: application/pdf" in pdf
    assert "Content-Type: application/octet-stream" in unknown


def test_send_document_caption_optional(tmp_path: Path) -> None:
    with serve_posts([_ok()]) as server:
        _send(server.url, _file(tmp_path))
    assert b"caption" not in server.requests[0].body


def test_send_document_caption_too_long_raises_before_any_request(tmp_path: Path) -> None:
    with serve_posts([]) as server, pytest.raises(ValueError, match="1024"):
        _send(server.url, _file(tmp_path), "x" * (delivery.MAX_CAPTION_CHARS + 1))
    assert server.requests == []


def test_send_document_transport_error_never_leaks_token(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="ConnectError") as exc:
        _send(closed_port_url(), _file(tmp_path))
    assert FAKE_BOT_TOKEN not in str(exc.value)


def test_send_document_http_error_keeps_status_only(tmp_path: Path) -> None:
    with (
        serve_posts([text_reply("Bad Request: file is too big", status=400)]) as server,
        pytest.raises(RuntimeError, match="HTTP 400") as exc,
    ):
        _send(server.url, _file(tmp_path))
    assert "file is too big" in str(exc.value)
    assert FAKE_BOT_TOKEN not in str(exc.value)


def test_send_document_api_error_body_reported(tmp_path: Path) -> None:
    reply = json_reply({"ok": False, "description": "chat not found"})
    with serve_posts([reply]) as server, pytest.raises(RuntimeError, match="chat not found"):
        _send(server.url, _file(tmp_path))


def test_send_document_unparseable_body_is_a_value_error(tmp_path: Path) -> None:
    with (
        serve_posts([text_reply("<html>proxy page</html>")]) as server,
        pytest.raises(ValueError, match="Expecting value"),
    ):
        _send(server.url, _file(tmp_path))


def test_main_missing_credentials_fails_before_sending(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with serve_posts([]) as server:
        code = send_file.main(
            [str(_file(tmp_path))], api_base=server.url, credentials=NO_CREDENTIALS
        )
    assert code == 1
    assert "MEIVA_TELEGRAM_BOT_TOKEN" in capsys.readouterr().err
    assert server.requests == []


def test_main_missing_file_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = send_file.main(
        [str(tmp_path / "nope.bin")], api_base=closed_port_url(), credentials=CREDENTIALS
    )
    assert code == 1
    assert "not a file" in capsys.readouterr().err


def test_main_over_long_caption_fails_before_sending(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = send_file.main(
        [str(_file(tmp_path)), "--caption", "x" * 2000],
        api_base=closed_port_url(),
        credentials=CREDENTIALS,
    )
    assert code == 1
    assert "caption exceeds 1024" in capsys.readouterr().err


def test_main_success_reports_message_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    p = _file(tmp_path, "report.pdf", b"x" * 10)
    with serve_posts([_ok(777)]) as server:
        code = send_file.main(
            [str(p), "--caption", "hi"], api_base=server.url, credentials=CREDENTIALS
        )
    assert code == 0
    assert capsys.readouterr().out == (
        f"sent report.pdf (10 bytes) to chat {OWNER}: message_id=777\n"
    )
    [post] = server.requests
    assert 'name="caption"' in _body(post) and "hi" in _body(post)
    assert 'filename="report.pdf"' in _body(post)


def test_main_reports_a_failed_send_as_one_error_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with serve_posts([text_reply("Unauthorized", status=401)]) as server:
        code = send_file.main([str(_file(tmp_path))], api_base=server.url, credentials=CREDENTIALS)
    captured = capsys.readouterr()
    assert code == 1 and captured.out == ""
    assert captured.err == "error: telegram sendDocument failed: HTTP 401 - Unauthorized\n"
