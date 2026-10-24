from __future__ import annotations

import pytest

from services.supervision.healthchecks import frontend as hc
from services.supervision.healthchecks.plans.frontend_plan import application_port
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


@pytest.mark.parametrize(
    ("entry_port", "app_port", "expected"),
    [
        (3000, 3001, 3001),
        (3000, None, 3001),
        (4100, 0, 4101),
        (None, None, 3001),
        (3000, 9000, 9000),
    ],
)
def test_the_application_port_is_the_explicit_one_or_the_entry_port_plus_one(
    entry_port: int | None, app_port: int | None, expected: int
) -> None:
    assert application_port(entry_port=entry_port, app_port=app_port) == expected
