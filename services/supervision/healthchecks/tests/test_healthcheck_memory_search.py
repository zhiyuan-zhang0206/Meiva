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
    observed = DaemonProbe.down("connection refused")
    result = hc.search_verdict(observed)

    assert result is observed
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


@pytest.mark.parametrize(
    ("status", "body", "alive", "detail"),
    [
        (200, b'{"hits": []}', True, "with a hits payload"),
        (200, b'{"nope": 1}', False, "without a hits payload"),
        (500, b"", False, "POST /search failed (HTTPStatusError:"),
        (200, b"not-json", False, "POST /search failed (JSONDecodeError:"),
        (200, b"null", False, "without an object payload"),
        (200, b"42", False, "without an object payload"),
    ],
)
def test_bound_probe_preserves_protocol_verdicts(
    status: int, body: bytes, alive: bool, detail: str
) -> None:
    with HttpStub(status=status, body=body) as service:
        result = hc.memory_search_probe(uri=service.url, embedding_name="gemini")

    assert result.alive is alive
    assert result.terminal is False
    assert detail in result.detail


def test_bound_probe_rejects_unknown_provider_before_network_io() -> None:
    with HttpStub() as service:
        with pytest.raises(ValueError, match="unknown embedding provider"):
            hc.memory_search_probe(uri=service.url, embedding_name="unknown-provider")

        assert service.requests == []


@pytest.mark.parametrize("payload", ["hits", ["hits"], None, 42])
def test_search_probe_rejects_non_object_json_even_when_it_contains_hits(payload: object) -> None:
    with HttpStub(body=json.dumps(payload).encode()) as service:
        result = hc.memory_search_probe(uri=service.url, embedding_name="gemini")

    assert result.alive is False
    assert result.terminal is False
    assert result.detail == "POST /search answered without an object payload"


@pytest.mark.parametrize("hits", [[], None, "unchanged-payload"])
def test_object_hits_keeps_the_existing_presence_contract(hits: object) -> None:
    with HttpStub(body=json.dumps({"hits": hits}).encode()) as service:
        result = hc.memory_search_probe(uri=service.url, embedding_name="gemini")

    assert result.alive is True
