# meiva:link cli/docs/operator-surfaces/public-client-wire.meiva.okf.md
import pytest

from cli.tests.wire.public_gateway import PublicGateway
from cli.tests.wire.public_gateway import wire as wire


@pytest.mark.parametrize("payload", [{"status": "enqueued"}, {"status": None}, {}])
def test_cancel_without_a_known_answer_never_prints_success(
    wire: PublicGateway, capsys: pytest.CaptureFixture[str], payload: dict[str, object]
) -> None:
    wire.respond("POST", payload)
    assert wire.run("agents", "cancel", "7", expect="value") == 0
    assert capsys.readouterr().out == ""
