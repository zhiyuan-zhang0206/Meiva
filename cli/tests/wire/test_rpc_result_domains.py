# meiva:link cli/docs/operator-surfaces/public-client-wire.meiva.okf.md
import pytest

from cli.tests.wire.public_gateway import PublicGateway
from cli.tests.wire.public_gateway import wire as wire


def test_cancel_reports_an_enqueued_request(
    wire: PublicGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    wire.respond("POST", {"status": "enqueued"})
    assert wire.run("agents", "cancel", "7") == 0
    assert capsys.readouterr().out.strip().endswith("agent 7 cancel: enqueued")


@pytest.mark.parametrize("payload", [{"status": "unknown"}, {"status": None}, {}])
def test_cancel_without_a_known_answer_never_prints_success(
    wire: PublicGateway, capsys: pytest.CaptureFixture[str], payload: dict[str, object]
) -> None:
    wire.respond("POST", payload)
    assert wire.run("agents", "cancel", "7", expect="value") == 0
    assert capsys.readouterr().out == ""
