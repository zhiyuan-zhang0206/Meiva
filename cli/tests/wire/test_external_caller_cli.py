# meiva:link cli/docs/operator-surfaces/public-client-wire.meiva.okf.md

import pytest

from base.api_contracts.idempotency import scoped_chat_message_key
from cli.tests.wire.public_gateway import PublicGateway
from cli.tests.wire.public_gateway import wire as wire


@pytest.fixture
def post(wire: PublicGateway) -> PublicGateway:
    wire.caller_profile = True
    wire.respond("POST", {"status": "enqueued"})
    return wire


@pytest.mark.parametrize(
    "source", ["user", "external_agent:claude_code", "external_agent:codex:run-42"]
)
def test_send_carries_explicit_source(post: PublicGateway, source: str) -> None:
    post.caller_profile = False
    assert post.run("agents", "send", "42", "hello", "--source", source) == 0
    assert post.requests[-1]["json"]["source"] == source


def test_send_honours_explicit_source_under_profile(post: PublicGateway) -> None:
    assert post.run("agents", "send", "42", "hello", "--source", "user") == 0
    assert post.requests[-1]["json"]["source"] == "user"


def test_missing_send_source_is_not_compensated(post: PublicGateway) -> None:
    assert post.run(mode="send-missing-source", expect="provenance", match="requires --source") == 0
    assert post.requests == []


def _send_keyed(*, source: str, key: str) -> tuple[str, ...]:
    return (
        "agents",
        "send",
        "42",
        "hello",
        "--source",
        source,
        "--client-message-id",
        key,
    )


# meiva:note
# The same caller key twice, from the same sender and target, is one message.
def test_client_message_id_names_one_message_for_one_sender(post: PublicGateway) -> None:
    send = _send_keyed(source="user", key="nightly-brief")
    assert post.run(*send) == 0
    assert post.run(*send) == 0
    keys = [request["headers"]["Idempotency-Key"] for request in post.requests[-2:]]
    assert keys[0] == keys[1] == scoped_chat_message_key("user", 42, "nightly-brief")


# meiva:note
# The cluster-wide key space is narrowed per sender, so reuse cannot swallow a send.
def test_one_client_message_id_from_two_senders_is_two_messages(post: PublicGateway) -> None:
    assert post.run(*_send_keyed(source="shell:7", key="nightly-brief")) == 0
    assert post.run(*_send_keyed(source="shell:8", key="nightly-brief")) == 0
    first, second = (request["headers"]["Idempotency-Key"] for request in post.requests[-2:])
    assert first == scoped_chat_message_key("shell:7", 42, "nightly-brief")
    assert second == scoped_chat_message_key("shell:8", 42, "nightly-brief")
    assert first != second


def test_a_send_without_a_client_message_id_still_mints_a_fresh_key(
    post: PublicGateway,
) -> None:
    assert post.run("agents", "send", "42", "hello", "--source", "user") == 0
    assert post.run("agents", "send", "42", "hello", "--source", "user") == 0
    keys = [request["headers"]["Idempotency-Key"] for request in post.requests[-2:]]
    assert keys[0] != keys[1]


def test_a_duplicate_reply_reports_the_delivery_instead_of_claiming_a_new_one(
    post: PublicGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    post.respond("POST", {"status": "idling", "inbound_id": 99, "duplicate": True})
    assert post.run(*_send_keyed(source="user", key="nightly-brief")) == 0
    assert "duplicate: already delivered as 99" in capsys.readouterr().out


def test_lifecycle_takes_explicit_source(post: PublicGateway) -> None:
    post.caller_profile = False
    assert post.run("agents", "restart", "42", "-m", "restarting", "--source", "user") == 0
    assert post.requests[-1]["json"] == {"message": "restarting", "source": "user"}
    assert post.run("agents", "resurrect", "42", "--source", "user", "--no-message") == 0
    assert post.requests[-1]["json"] == {"resurrected_by": "user"}
    assert post.run("agents", "kill", "42", "--source", "shell:7", "--no-message") == 0
    assert post.requests[-1]["json"] == {"force": True, "source": "shell:7"}


def test_lifecycle_never_reads_profile(post: PublicGateway) -> None:
    assert post.run("agents", "restart", "42", "-m", "restarting") == 0
    assert post.requests[-1]["json"] == {"message": "restarting"}
    assert post.run("agents", "resurrect", "42", "--no-message") == 0
    assert post.requests[-1]["body"] == b""
    assert post.run("agents", "kill", "42", "--no-message") == 0
    assert post.requests[-1]["json"] == {"force": True}


# meiva:note
# The key is validated where `--source` is: a key the wire cannot carry never reaches a write.
def test_send_rejects_a_client_message_id_the_wire_cannot_carry(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from cli.parsers import build_parser

    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(
            ["agents", "send", "1", "hi", "--source", "user", "--client-message-id", "x" * 129]
        )
    assert exit_info.value.code == 2
    assert "1 to 128 characters" in capsys.readouterr().err


def test_send_requires_source_at_parse_time(capsys: pytest.CaptureFixture[str]) -> None:
    from cli.parsers import build_parser

    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["agents", "send", "1", "hi"])
    assert exit_info.value.code == 2
    assert "--source" in capsys.readouterr().err


def test_send_accepts_declared_external_agent_at_parse_time() -> None:
    from cli.parsers import build_parser

    args = build_parser().parse_args(
        ["agents", "send", "1", "hi", "--source", "external_agent:claude_code:s1"]
    )
    assert args.source == "external_agent:claude_code:s1"


@pytest.mark.parametrize("source", ["claude_code", "external_agent:", "external_agent:a b"])
def test_send_rejects_malformed_external_agent_at_parse_time(
    capsys: pytest.CaptureFixture[str], source: str
) -> None:
    from cli.parsers import build_parser

    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["agents", "send", "1", "hi", "--source", source])
    assert exit_info.value.code == 2
    assert "source" in capsys.readouterr().err


def test_send_rejects_unknown_source_at_parse_time(capsys: pytest.CaptureFixture[str]) -> None:
    from cli.parsers import build_parser

    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["agents", "send", "1", "hi", "--source", "bogus"])
    assert exit_info.value.code == 2
    assert "Unrecognized inbound source" in capsys.readouterr().err


def test_restart_rejects_bad_config_at_parse_time(capsys: pytest.CaptureFixture[str]) -> None:
    from cli.parsers import build_parser

    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(
            ["agents", "restart", "1", "--config", "{oops", "-m", "restarting"]
        )
    assert exit_info.value.code == 2
    assert "invalid config JSON" in capsys.readouterr().err


def test_restart_rejects_non_object_config_at_parse_time(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from cli.parsers import build_parser

    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(
            ["agents", "restart", "1", "--config", "[1, 2]", "-m", "restarting"]
        )
    assert exit_info.value.code == 2
    assert "config must be a JSON object" in capsys.readouterr().err


def test_send_without_provenance_exits_cleanly(
    post: PublicGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    post.caller_profile = False
    assert post.run(mode="send-handler-missing") == 2
    err = capsys.readouterr().err
    assert "requires --source" in err
    assert "--source user" in err
    assert post.requests == []


def test_send_invalid_source_exits_cleanly(
    post: PublicGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    post.caller_profile = False
    assert post.run(mode="send-handler-invalid") == 2
    assert "Unrecognized inbound source" in capsys.readouterr().err
    assert post.requests == []


def test_lifecycle_invalid_source_exits_cleanly(
    post: PublicGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    assert post.run(mode="kill-handler-invalid") == 2
    assert "Unrecognized inbound source" in capsys.readouterr().err
    assert post.requests == []
