from __future__ import annotations

import pytest

from services.supervision.healthchecks import frontend as hc
from services.supervision.healthchecks.tests.http_stub import HttpStub


def test_application_http_answer_controls_availability() -> None:
    with HttpStub(status=200) as app:
        assert hc.probe_frontend_at(app.port).alive is True
        assert app.requests == [("GET", "/", b"")]


@pytest.mark.parametrize("status", [404, 500, 503])
def test_a_non_2xx_answer_is_down(status: int) -> None:
    with HttpStub(status=status) as app:
        assert not hc.probe_frontend_at(app.port).alive


def test_nothing_listening_is_down() -> None:
    with HttpStub() as app:
        port = app.port

    assert not hc.probe_frontend_at(port).alive


def test_a_missing_curl_is_down() -> None:
    with HttpStub() as app:
        assert not hc.probe_frontend_at(app.port, curl_binary="curl-that-does-not-exist").alive


def test_an_application_that_never_answers_is_down_within_the_timeout() -> None:
    with HttpStub(hang=True) as app:
        assert not hc.probe_frontend_at(app.port, timeout_s=0.5).alive
