from __future__ import annotations

import json

import pytest

from base.daemon.health import DaemonProbe
from services.supervision.healthchecks import memory_search as hc
from services.supervision.healthchecks.tests.http_stub import HttpStub


def test_probe_up_when_search_answers_with_hits() -> None:
    result = hc.search_verdict({"hits": []})

    assert result.alive is True
    assert result.terminal is False


def test_probe_down_when_search_fails() -> None:
    result = hc.search_verdict(DaemonProbe.down("connection refused"))

    assert result.alive is False
    assert "connection refused" in result.detail


def test_probe_down_when_payload_lacks_hits() -> None:
    """A foreign process answering 200 on the port is not the search service."""
    assert hc.search_verdict({"nope": 1}).alive is False


def test_search_probe_sends_a_zero_vector_sized_by_the_provider_descriptor() -> None:
    from services.derived.memory_indexer.embeddings import factory

    descriptor = factory.get_descriptor("gemini")
    with HttpStub(body=b'{"hits": []}') as service:
        assert hc.post_search(service.url, embedding_name="gemini") == {"hits": []}

        ((method, path, body),) = service.requests
        assert (method, path) == ("POST", "/search")
        assert json.loads(body) == {"vector": [0.0] * descriptor.dim, "k": 1}


def test_a_failing_search_is_a_not_alive_verdict() -> None:
    with HttpStub(status=500) as service:
        answer = hc.post_search(service.url, embedding_name="gemini")

    assert isinstance(answer, DaemonProbe)
    assert answer.alive is False
    assert "POST /search failed" in answer.detail


def test_unknown_provider_fails_before_search_request() -> None:
    with HttpStub() as service:
        with pytest.raises(ValueError, match="unknown embedding provider"):
            hc.post_search(service.url, embedding_name="unknown-provider")

        assert service.requests == []
