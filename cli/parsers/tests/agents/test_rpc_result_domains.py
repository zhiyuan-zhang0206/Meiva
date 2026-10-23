import httpx
import pytest

from cli.commands.agents import control


def _transport(payload: dict[str, object]) -> control.AgentsTransport:
    response = httpx.Response(200, json=payload, request=httpx.Request("POST", "http://gateway"))

    def post(*_args: object, **_kwargs: object) -> httpx.Response:
        return response

    return control.AgentsTransport(
        gateway_base=lambda: "http://gateway",
        headers=dict,
        post=post,
    )


@pytest.mark.parametrize("payload", [{"status": "enqueued"}, {"status": None}, {}])
def test_cancel_without_a_known_answer_never_prints_success(
    capsys: pytest.CaptureFixture[str], payload: dict[str, object]
) -> None:
    with pytest.raises(ValueError):
        control.cmd_agents_cancel(7, transport=_transport(payload))
    assert capsys.readouterr().out == ""
