# meiva:link cli/docs/operator-surfaces/public-client-wire.meiva.okf.md

import pytest

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


def test_lifecycle_takes_explicit_source(post: PublicGateway) -> None:
    post.caller_profile = False
    assert post.run("agents", "restart", "42", "--source", "user") == 0
    assert post.requests[-1]["json"] == {"source": "user"}
    assert post.run("agents", "resurrect", "42", "--source", "user") == 0
    assert post.requests[-1]["json"] == {"resurrected_by": "user"}
    assert post.run("agents", "kill", "42", "--source", "shell:7") == 0
    assert post.requests[-1]["json"] == {"force": True, "source": "shell:7"}


def test_lifecycle_never_reads_profile(post: PublicGateway) -> None:
    assert post.run("agents", "restart", "42") == 0
    assert post.requests[-1]["body"] == b""
    assert post.run("agents", "resurrect", "42") == 0
    assert post.requests[-1]["body"] == b""
    assert post.run("agents", "kill", "42") == 0
    assert post.requests[-1]["json"] == {"force": True}


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
        build_parser().parse_args(["agents", "restart", "1", "--config", "{oops"])
    assert exit_info.value.code == 2
    assert "invalid config JSON" in capsys.readouterr().err


def test_restart_rejects_non_object_config_at_parse_time(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from cli.parsers import build_parser

    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["agents", "restart", "1", "--config", "[1, 2]"])
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
