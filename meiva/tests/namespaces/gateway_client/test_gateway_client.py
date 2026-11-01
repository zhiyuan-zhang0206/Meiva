"""meiva/gateway_client/__init__.py tests.

Covers:
- raise_from_response: wire contract error reconstruction
- post / get: retry logic + network-layer errors → GatewayUnavailable
- spawn / send_message: public API happy + error paths
"""

import httpx
import pytest

from base.agents.context import MeivaContext
from base.agents.context.gateway_client import memory_search as memory_client
from base.agents.context.identity import AgentIdentity
from base.api_contracts.agent_contract import GatewayUnavailable
from meiva import gateway_client
from meiva.tests.fixtures.gateway_wire import (
    CLIENT_TIMEOUT_SECONDS,
    GatewayStub,
    GatewayWire,
    Reply,
)
from meiva.tests.fixtures.gateway_wire import bound_wire as bound_wire
from meiva.tests.fixtures.gateway_wire import gateway_stub as gateway_stub
from meiva.tests.fixtures.gateway_wire import wire as wire

_REQUEST = httpx.Request("POST", "http://gw/api/agents/42/messages")


def _response(status: int, *, json: object = None, text: str | None = None) -> httpx.Response:
    return httpx.Response(status, json=json, text=text, request=_REQUEST)


# --- raise_from_response ---


class TestRaiseFromResponse:
    def test_success_does_not_raise(self):
        from base.agents.context.gateway_client.transport import raise_from_response

        raise_from_response(_response(200, json={}))

    def test_wire_json_error_reconstructed(self):
        """HTTP 400 + body {"reason":"agent_not_found","detail":"..."} → AgentNotFound."""
        from base.agents.context.gateway_client.transport import raise_from_response
        from base.api_contracts.agent_contract import AgentNotFound

        resp = _response(404, json={"reason": "agent_not_found", "detail": "agent 99 not found"})

        with pytest.raises(AgentNotFound, match="agent 99 not found"):
            raise_from_response(resp)

    def test_non_json_body_falls_through_to_http_error(self):
        """body is not JSON → raise_for_status raises HTTPStatusError."""
        from base.agents.context.gateway_client.transport import raise_from_response

        with pytest.raises(httpx.HTTPStatusError):
            raise_from_response(_response(500, text="Internal Server Error"))

    # meiva:note
    # `httpx` reads a response eagerly, so a body whose Content-Encoding fails to decode makes the
    # client call itself fail; the case this regression guards is a reply whose `.json()` raises
    # the decoding error, which only a response of this shape can produce on demand.
    def test_corrupted_content_encoding_falls_through(self):
        """A response body whose Content-Encoding fails to decode raises
        httpx.DecodingError (0.28.1, broken gzip/br stream) — same
        protocol-mismatch class as non-JSON: falls through to the clean
        HTTPStatusError instead of the DecodingError masking the status.

        Regression: _wire_reason only caught JSONDecodeError (task #1669).
        """
        from base.agents.context.gateway_client.transport import raise_from_response

        class UndecodableResponse(httpx.Response):
            def json(self, **kwargs: object) -> object:
                raise httpx.DecodingError("corrupted gzip stream")

        resp = UndecodableResponse(502, request=_REQUEST)

        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            raise_from_response(resp)
        # The status was surfaced as the primary error — the DecodingError
        # never escapes as the exception seen by the caller.
        assert excinfo.value.response.status_code == 502
        assert not isinstance(excinfo.value.__context__, httpx.DecodingError)

    def test_missing_reason_field_falls_through(self):
        """JSON present but missing reason field → raise_for_status."""
        from base.agents.context.gateway_client.transport import raise_from_response

        with pytest.raises(httpx.HTTPStatusError):
            raise_from_response(_response(400, json={"detail": "something"}))

    def test_invalid_reason_value_falls_through(self):
        """reason value not in ErrorReason enum → ValueError → raise_for_status."""
        from base.agents.context.gateway_client.transport import raise_from_response

        with pytest.raises(httpx.HTTPStatusError):
            raise_from_response(_response(400, json={"reason": "garbage_value", "detail": "x"}))

    def test_valid_reason_missing_detail_falls_through(self):
        """Valid `reason` but `detail` field missing → HTTPStatusError, not
        KeyError: 'detail' masking the status code (same class as task #1205).

        Regression: the reverse-lookup raise indexed `body["detail"]` without
        a guard, so a reason-bearing body with no detail leaked a raw KeyError
        and the HTTP status code never reached the caller.
        """
        from base.agents.context.gateway_client.transport import raise_from_response

        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            raise_from_response(_response(404, json={"reason": "agent_not_found"}))
        assert excinfo.value.response.status_code == 404
        assert not isinstance(excinfo.value.__context__, KeyError)

    def test_valid_reason_non_string_detail_falls_through(self):
        """Valid `reason` but non-string `detail` → HTTPStatusError with the
        status code; a malformed detail is a protocol mismatch, not an
        application error to reconstruct."""
        from base.agents.context.gateway_client.transport import raise_from_response

        resp = _response(404, json={"reason": "agent_not_found", "detail": {"nested": True}})

        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            raise_from_response(resp)
        assert excinfo.value.response.status_code == 404
        assert not isinstance(excinfo.value.__context__, KeyError)

    def test_503_without_reason_raises_http_error_with_status(self):
        """A real 503 with a JSON body lacking `reason` raises HTTPStatusError
        carrying the status code — not a KeyError chained over it (task #1205).

        Regression: raise_for_status used to run inside the `except KeyError`
        handler, so the traceback led with `KeyError: 'reason'` (the gateway's
        FastAPI-default body has no wire `reason`) and the 503 status was
        buried in the exception chain instead of being the primary error.
        """
        from base.agents.context.gateway_client.transport import raise_from_response

        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            raise_from_response(_response(503, json={"detail": "gateway unavailable"}))
        assert excinfo.value.response.status_code == 503
        # The HTTP error is the primary exception: no parse KeyError chained
        # as __context__ masking the original status code.
        assert not isinstance(excinfo.value.__context__, KeyError)

    def test_non_object_json_body_falls_through(self):
        """JSON body that is not an object (no `reason` possible) → raise_for_status."""
        from base.agents.context.gateway_client.transport import raise_from_response

        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            raise_from_response(_response(503, json=["not", "an", "object"]))
        assert excinfo.value.response.status_code == 503
        assert not isinstance(excinfo.value.__context__, KeyError)


# --- post retry ---


class TestPostRetry:
    def test_first_attempt_succeeds(self, wire: GatewayWire):
        from base.agents.context.gateway_client.transport import post

        wire.stub.reply("POST", "/api/memory/search", {"results": []})

        result = post("/api/memory/search", {"key": "val"}, context=wire.context)
        assert result.json() == {"results": []}
        assert [(s.method, s.path, s.body) for s in wire.sent] == [
            ("POST", "/api/memory/search", {"key": "val"})
        ]

    def test_retries_on_transport_error(self, wire: GatewayWire):
        from base.agents.context.gateway_client.transport import post

        wire.stub.reply("POST", "/api/memory/search", {"results": []})
        # First two fail, third succeeds; a read-only POST endpoint, so a read
        # timeout is safe to retry.
        wire.fail(httpx.ConnectError)
        wire.fail(httpx.ReadTimeout, delivered=True)

        result = post("/api/memory/search", context=wire.context)
        assert result.status_code == 200
        assert len(wire.sent) == 3
        assert len(wire.stub.received) == 2

    def test_all_retries_exhausted_raises_gateway_unavailable(self, wire: GatewayWire):
        from base.agents.context.gateway_client.transport import post

        wire.fail(httpx.ConnectError, count=3)

        with pytest.raises(GatewayUnavailable, match="after 3 retries"):
            post("/api/memory/search", context=wire.context)
        assert len(wire.sent) == 3
        assert wire.stub.received == []

    def test_http_4xx_not_retried(self, wire: GatewayWire):
        """HTTP 4xx is an application error, no retry."""
        from base.agents.context.gateway_client.transport import post, raise_from_response
        from base.api_contracts.agent_contract import AgentNotFound

        wire.stub.reply(
            "POST",
            "/api/memory/search",
            {"reason": "agent_not_found", "detail": "x"},
            status=404,
        )

        # post returns the response, raise_from_response is called by the caller
        result = post("/api/memory/search", context=wire.context)
        assert result.status_code == 404
        assert len(wire.sent) == 1  # no retry
        with pytest.raises(AgentNotFound):
            raise_from_response(result)


# --- post timeout contract ---


class TestPostTimeoutContract:
    """What `post` hands httpx, not what it was asked for.

    httpx distinguishes three things a caller can mean by `timeout`: a value
    (use it), the `USE_CLIENT_DEFAULT` sentinel (fall back to the client's
    configured timeout), and `None` (**never** time out). Only the sentinel
    falls back, so these assert the argument httpx actually receives — asserting
    `post`'s own default would have passed all along while every POST in the
    SDK ran unbounded and `MEIVA_GATEWAY_HTTP_TIMEOUT_SECONDS` did nothing.
    """

    def test_default_defers_to_client_timeout(self, wire: GatewayWire):
        """No per-call timeout → httpx gets the sentinel, never None."""
        from base.agents.context.gateway_client.transport import post

        wire.stub.reply("POST", "/api/memory/search", {"results": []})

        post("/api/memory/search", {"query": "x", "k": 5}, context=wire.context)

        assert wire.sent[0].timeout["read"] == CLIENT_TIMEOUT_SECONDS

    def test_explicit_timeout_is_forwarded(self, wire: GatewayWire):
        """A per-call timeout still overrides the client default."""
        from base.agents.context.gateway_client.transport import post

        wire.stub.reply("POST", "/api/agents/1/messages", None, status=201)

        post(
            "/api/agents/1/messages",
            {"content": "hi"},
            timeout=httpx.Timeout(120.0),
            context=wire.context,
        )

        assert wire.sent[0].timeout["read"] == 120.0


# --- get retry ---


class TestGetRetry:
    def test_first_attempt_succeeds(self, wire: GatewayWire):
        from base.agents.context.gateway_client.transport import get

        wire.stub.reply("GET", "/api/agents/1", {"id": 1})

        result = get("/api/agents/1", context=wire.context)
        assert result.json() == {"id": 1}
        assert [(s.method, s.path) for s in wire.sent] == [("GET", "/api/agents/1")]

    def test_all_retries_exhausted_raises_gateway_unavailable(self, wire: GatewayWire):
        from base.agents.context.gateway_client.transport import get

        wire.fail(httpx.ConnectError, count=3)

        with pytest.raises(GatewayUnavailable):
            get("/api/agents/1", context=wire.context)
        assert len(wire.sent) == 3


# --- spawn ---


class TestSendMessage:
    def test_send_message_fire_and_forget(self, bound_wire: GatewayWire):
        """send_message is pure POST + return, does not read the status field."""
        from meiva.gateway_client import send_message

        bound_wire.stub.reply("POST", "/api/agents/42/messages", None, status=201)

        result = send_message(42, content="hello", source="user")
        assert result is None
        [sent] = bound_wire.sent
        assert (sent.method, sent.path) == ("POST", "/api/agents/42/messages")
        assert sent.body == {"content": "hello", "source": "user"}
        assert sent.timeout["read"] == 120.0


# --- terminate / restart ---


class TestLifecycle:
    @pytest.mark.parametrize("wire_status", ["enqueued", "already_terminated"])
    def test_terminate_returns_status(self, bound_wire: GatewayWire, wire_status: str):
        from meiva.gateway_client import terminate

        bound_wire.stub.reply(
            "POST", "/api/agents/42/terminate", {"status": wire_status, "open_tasks": None}
        )

        result = terminate(42, source="agent:1", force=True)
        # The client hands back the full wire payload — status plus the
        # open-tasks hint — no longer just the status string (task #3361).
        assert result == {"status": wire_status, "open_tasks": None}
        assert bound_wire.sent[0].body == {"source": "agent:1", "force": True}

    def test_terminate_passes_open_tasks_hint_through(self, bound_wire: GatewayWire):
        """A non-null hint rides the client verbatim; the SDK converts it."""
        from meiva.gateway_client import terminate

        hint = {
            "count": 6,
            "tasks": [
                {
                    "id": 11,
                    "title": "sync terminate tests",
                    "status": "in_progress",
                    "updated_at": "2026-09-14T05:00:00+00:00",
                }
            ],
            "more": 1,
        }
        bound_wire.stub.reply(
            "POST", "/api/agents/42/terminate", {"status": "enqueued", "open_tasks": hint}
        )

        result = terminate(42, source="agent:1")
        assert result == {"status": "enqueued", "open_tasks": hint}

    def test_restart_returns_status(self, bound_wire: GatewayWire):
        from dataclasses import replace

        from meiva import agents
        from meiva.gateway_client import restart
        from meiva.sdk_surface import process_context

        bound_wire.stub.reply("POST", "/api/agents/42/restart", {"status": "idling"})
        bound_wire.stub.reply("POST", "/api/agents/43/restart", {"status": "enqueued"})

        status = restart(42, source="agent:1", message="restarting agent")
        assert status == "idling"
        assert bound_wire.sent[0].body == {"source": "agent:1", "message": "restarting agent"}

        process_context.bind_context(replace(bound_wire.context, identity=AgentIdentity(1, True)))
        assert agents.restart(43, message="Zhiyuan reloading skill") == "enqueued"
        assert bound_wire.sent[1].body == {
            "source": "agent:1",
            "message": "Zhiyuan reloading skill",
        }


# meiva:note
# The verb of a request plan reaches the wire as that verb, for each verb the client sends.
class TestRequestMethods:
    def test_a_get_plan_is_sent_as_a_get(self, bound_wire: GatewayWire):
        bound_wire.stub.reply("GET", "/api/agents/42", {"id": 42})

        assert gateway_client.get_agent(42) == {"id": 42}

        [sent] = bound_wire.sent
        assert (sent.method, sent.path, sent.body) == ("GET", "/api/agents/42", None)

    def test_a_patch_plan_is_sent_as_a_patch(self, bound_wire: GatewayWire):
        bound_wire.stub.reply("PATCH", "/api/agents/42", None, status=204)

        gateway_client.patch_label(42, "lead")

        [sent] = bound_wire.sent
        assert (sent.method, sent.path, sent.body) == (
            "PATCH",
            "/api/agents/42",
            {"label": "lead", "source": "self"},
        )

    def test_a_post_plan_is_sent_as_a_post(self, bound_wire: GatewayWire):
        bound_wire.stub.reply("POST", "/api/agents/42/cancel", {"status": "cancelled"})

        assert gateway_client.cancel(42) == "cancelled"

        [sent] = bound_wire.sent
        assert (sent.method, sent.path, sent.body) == ("POST", "/api/agents/42/cancel", {})


# --- transient HTTP 429/5xx retry (task #960) ---

_FASTAPI_DEFAULT_500 = Reply(500, "Internal Server Error")
_NO_RESULTS: dict[str, list[object]] = {"results": []}
_ONE_RESULT: dict[str, list[object]] = {
    "results": [{"path": "a.md", "description": "", "tags": [], "score": 0.5, "chunk": "c"}]
}


class TestTransientHttpRetry:
    """Idempotent requests retry known transient statuses with bounded backoff."""

    def test_memory_search_unknown_500_is_exposed_once(self, wire: GatewayWire):
        """An unhandled server failure must not be retried as backend congestion."""
        from base.agents.context.gateway_client.memory_search import memory_search

        wire.stub.reply_in_turn(
            "POST", "/api/memory/search", [_FASTAPI_DEFAULT_500, Reply(200, _NO_RESULTS)]
        )

        with pytest.raises(httpx.HTTPStatusError):
            memory_search("query", 5, purpose="search", context=wire.context)
        assert len(wire.sent) == 1

    def test_memory_search_dedicated_timeout_and_single_retry(self, wire: GatewayWire):
        """Memory search carries its own budget (task #2003/A): the
        per-attempt timeout is the gateway's search deadline + margin (not
        the global MEIVA_GATEWAY_HTTP_TIMEOUT_SECONDS), and a persistent 503 is
        exhausted after ONE retry instead of stacking the default 3 — the
        gateway's own deadline has already spent that time, so re-sending only
        re-queues behind the same congestion."""
        from base.api_contracts.agent_contract import IndexerUnavailable

        wire.stub.reply(
            "POST",
            "/api/memory/search",
            {"reason": "indexer_unavailable", "detail": "busy"},
            status=503,
        )

        with pytest.raises(IndexerUnavailable, match="busy"):
            memory_client.memory_search("query", 5, purpose="search", context=wire.context)
        assert len(wire.sent) == 2

        # Per-attempt timeout derives from the gateway deadline + 3s margin.
        wire.stub.reply("POST", "/api/memory/search", _ONE_RESULT)
        memory_client.memory_search("query", 5, purpose="search", context=wire.context)
        assert wire.sent[-1].timeout["read"] == 15.0 + memory_client.MEMORY_SEARCH_TIMEOUT_MARGIN_S

    def test_memory_search_sends_the_purpose(self, wire: GatewayWire):
        wire.stub.reply("POST", "/api/memory/search", _NO_RESULTS)
        memory_client.memory_search("query", 5, purpose="recall", context=wire.context)
        assert wire.sent[0].body == {"query": "query", "k": 5, "purpose": "recall"}

    def test_memory_search_maps_the_wire_result(self, wire: GatewayWire):
        """Every wire field lands on `MemorySearchResult`, tags as a tuple."""
        wire.stub.reply(
            "POST",
            "/api/memory/search",
            {
                "results": [
                    {
                        "path": "n/a.md",
                        "description": "d",
                        "tags": ["type/user", "x"],
                        "score": 0.75,
                        "chunk": "the passage",
                    }
                ]
            },
        )
        assert memory_client.memory_search("query", 5, purpose="search", context=wire.context) == [
            memory_client.MemorySearchResult("n/a.md", "d", ("type/user", "x"), 0.75, "the passage")
        ]

    def test_memory_search_caller_timeout_travels_in_the_request(self, wire: GatewayWire):
        """An explicit `timeout` is the caller's deadline: the gateway is told, and the HTTP
        timeout sits a margin above it so the gateway's own answer arrives first."""
        wire.stub.reply("POST", "/api/memory/search", _NO_RESULTS)
        memory_client.memory_search("query", 5, purpose="search", timeout=9.0, context=wire.context)
        [sent] = wire.sent
        assert sent.timeout["read"] == 9.0 + memory_client.MEMORY_SEARCH_TIMEOUT_MARGIN_S
        assert sent.body["deadline_seconds"] == 9.0

    def test_memory_search_without_a_timeout_sends_no_deadline(self, wire: GatewayWire):
        wire.stub.reply("POST", "/api/memory/search", _NO_RESULTS)
        memory_client.memory_search("query", 5, purpose="search", context=wire.context)
        assert "deadline_seconds" not in wire.sent[0].body

    def test_memory_search_sends_the_exclusion_sorted_and_only_when_given(self, wire: GatewayWire):
        wire.stub.reply("POST", "/api/memory/search", _NO_RESULTS)
        memory_client.memory_search(
            "query", 3, purpose="recall", exclude={"b.md", "a/c.md", "a.md"}, context=wire.context
        )
        memory_client.memory_search("query", 3, purpose="recall", exclude=(), context=wire.context)
        with_exclusion, without = wire.sent
        assert with_exclusion.body["exclude"] == ["a.md", "a/c.md", "b.md"]
        assert "exclude" not in without.body

    def test_memory_search_attempts_bound_the_retries(self, wire: GatewayWire):
        """A caller that gives up on its own deadline asks for one attempt: a retry would start
        a second search after it stopped waiting for the first."""
        from base.api_contracts.agent_contract import IndexerUnavailable

        wire.stub.reply(
            "POST",
            "/api/memory/search",
            {"reason": "indexer_unavailable", "detail": "busy"},
            status=503,
        )
        with pytest.raises(IndexerUnavailable):
            memory_client.memory_search(
                "query", 5, purpose="recall", attempts=1, context=wire.context
            )
        assert len(wire.sent) == 1

    def test_transient_5xx_exhausted_surfaces_wire_error(self, wire: GatewayWire):
        """After retries are exhausted the wire contract is preserved: a 503
        with reason indexer_unavailable raises IndexerUnavailable (the error
        callers catch to degrade), not a generic transport error."""
        from base.agents.context.gateway_client.transport import post, raise_from_response
        from base.api_contracts.agent_contract import IndexerUnavailable

        wire.stub.reply(
            "POST",
            "/api/memory/search",
            {"reason": "indexer_unavailable", "detail": "embed failed"},
            status=503,
        )

        resp = post("/api/memory/search", {"query": "q", "k": 5}, context=wire.context)
        assert len(wire.sent) == 3
        with pytest.raises(IndexerUnavailable, match="embed failed"):
            raise_from_response(resp)

    def test_transient_5xx_not_retried_for_non_idempotent(self, bound_wire: GatewayWire):
        """spawn is non-idempotent: an HTTP 5xx means the route may have
        committed (agent row created) before erroring — no re-send."""
        from meiva.gateway_client import spawn

        bound_wire.stub.reply_in_turn("POST", "/api/keyed/v1/agents", [_FASTAPI_DEFAULT_500])

        with pytest.raises(httpx.HTTPStatusError):
            spawn(
                spawner="user",
                prompt="hello",
                fork_from=None,
                prompt_source="user",
                idempotency_key="intent",
            )
        assert len(bound_wire.sent) == 1

    def test_get_transient_5xx_retried(self, wire: GatewayWire):
        from base.agents.context.gateway_client.transport import get

        wire.stub.reply_in_turn("GET", "/api/agents", [Reply(503, {}), Reply(200, {"ok": True})])

        resp = get("/api/agents", context=wire.context)
        assert resp.json() == {"ok": True}
        assert len(wire.sent) == 2

    def test_429_retried(self, wire: GatewayWire):
        from base.agents.context.gateway_client.transport import get

        wire.stub.reply_in_turn("GET", "/api/agents", [Reply(429, {}), Reply(200, {"ok": True})])

        resp = get("/api/agents", context=wire.context)
        assert resp.json() == {"ok": True}
        assert len(wire.sent) == 2


class TestRetryBackoffJitter:
    """Bounded exponential backoff + deterministic per-agent jitter
    (heartbeat-daemon de-phasing pattern, task #960)."""

    @staticmethod
    def _agent(agent_id: int) -> MeivaContext:
        return MeivaContext(identity=AgentIdentity(agent_id=agent_id, owns_loop=True))

    def test_agent_jitter_zero_without_agent_id(self) -> None:
        from base.agents.context.gateway_client import transport as gc

        assert gc.agent_jitter_seconds(context=MeivaContext()) == 0.0

    def test_agent_jitter_deterministic_and_bounded(self) -> None:
        from base.agents.context.gateway_client import transport as gc

        context = self._agent(1234)
        assert gc.agent_jitter_seconds(context=context) == gc.agent_jitter_seconds(
            context=context
        )  # deterministic
        assert 0.0 <= gc.agent_jitter_seconds(context=context) < gc.JITTER_SPAN_S

    def test_agent_jitter_differs_across_agents(self) -> None:
        from base.agents.context.gateway_client import transport as gc

        assert gc.agent_jitter_seconds(context=self._agent(1)) != gc.agent_jitter_seconds(
            context=self._agent(2)
        )

    def test_backoff_bounded_exponential(self) -> None:
        """1s → 2s → 4s → 8s cap (defaults), no jitter without an agent id."""
        from base.agents.context.clients import ClientSet
        from base.agents.context.gateway_client import transport as gc

        context = MeivaContext(
            clients=ClientSet(
                factories={
                    gc.GatewayTransportInputs: lambda: gc.GatewayTransportInputs(
                        max_retries_reader=lambda: 3,
                        retry_delay_reader=lambda: 1.0,
                        memory_deadline_reader=lambda: 15.0,
                    )
                }
            )
        )
        assert [gc.retry_delay_seconds(i, context) for i in range(6)] == [
            1.0,
            2.0,
            4.0,
            8.0,
            8.0,
            8.0,
        ]

    # meiva:note
    # Retries wait the bounded backoff schedule: the gap before the next attempt is at least the
    # delay of the attempt that failed, doubling, with no jitter without an agent.
    def test_requests_follow_the_backoff_schedule(self, gateway_stub: GatewayStub) -> None:
        from base.agents.context.gateway_client.transport import post

        with GatewayWire.open(gateway_stub, retry_delay_seconds=0.05) as slow:
            slow.fail(httpx.ConnectError, count=3)
            with pytest.raises(GatewayUnavailable):
                post("/api/x", context=slow.context)
            first, second, third = (sent.at for sent in slow.sent)
        assert second - first >= 0.05 - 0.005
        assert third - second >= 0.10 - 0.005


class TestSendMessageAtLeastOnceWithKey:
    """send_message is a pure INSERT behind an AtLeastOnceWithKey doorplate
    (R3 door ①): every retry of one logical message carries the same
    `Idempotency-Key` header and the server dedups, so the full transient
    family is retried safely — no duplicate inbound even when a retry lands
    after the first attempt already committed."""

    @pytest.mark.parametrize(
        ("error", "delivered"),
        [
            (httpx.ReadTimeout, True),
            (httpx.WriteTimeout, False),
            (httpx.ReadError, True),
        ],
    )
    def test_send_message_retries_transport_failure_with_one_key(
        self, bound_wire: GatewayWire, error: type[httpx.TransportError], delivered: bool
    ):
        from meiva.gateway_client import GatewayUnavailable, send_message

        bound_wire.fail(error, delivered=delivered, count=3)

        with pytest.raises(GatewayUnavailable, match="after 3 retries"):
            send_message(42, content="hello", source="user")
        assert len(bound_wire.sent) == 3
        assert len(bound_wire.stub.received) == (3 if delivered else 0)
        keys = {sent.headers["idempotency-key"] for sent in bound_wire.sent}
        assert len(keys) == 1, "all retries of one message must share one key"
        assert next(iter(keys))

    def test_send_message_unknown_500_is_exposed_once(self, bound_wire: GatewayWire):
        from meiva.gateway_client import send_message

        bound_wire.stub.reply_in_turn("POST", "/api/agents/42/messages", [_FASTAPI_DEFAULT_500])

        with pytest.raises(httpx.HTTPStatusError):
            send_message(42, content="hello", source="user")
        assert len(bound_wire.sent) == 1

    def test_send_message_503_without_reason_surfaces_status_code(self, bound_wire: GatewayWire):
        """Regression for task #1205 (2026-08-12 cluster-update report): a
        gateway 503 with a FastAPI-default body (no wire `reason`) must raise
        HTTPStatusError carrying 503 — not `KeyError: 'reason'` first, with
        the status code masked."""
        from meiva.gateway_client import send_message

        bound_wire.stub.reply(
            "POST", "/api/agents/42/messages", {"detail": "gateway unavailable"}, status=503
        )

        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            send_message(42, content="hello", source="user")
        assert excinfo.value.response.status_code == 503
        assert len(bound_wire.sent) == 3  # AtLeastOnceWithKey retries, deduped by key
        assert not isinstance(excinfo.value.__context__, KeyError)

    def test_send_message_sends_key_header(self, bound_wire: GatewayWire):
        from meiva.gateway_client import send_message

        bound_wire.stub.reply("POST", "/api/agents/42/messages", None, status=201)

        send_message(42, content="hello", source="user")
        assert bound_wire.sent[0].headers.get("idempotency-key")


def test_retry_settings_are_read_when_a_request_retries() -> None:
    """The attempt count and base delay come from the settings at use, not from values
    captured when the module imported."""
    from base.agents.context.clients import ClientSet
    from base.agents.context.gateway_client import transport

    values = {"attempts": 3.0, "delay": 1.0}
    context = MeivaContext(
        clients=ClientSet(
            factories={
                transport.GatewayTransportInputs: lambda: transport.GatewayTransportInputs(
                    max_retries_reader=lambda: int(values["attempts"]),
                    retry_delay_reader=lambda: values["delay"],
                    memory_deadline_reader=lambda: 15.0,
                )
            }
        )
    )
    values.update(attempts=7, delay=0.5)
    assert transport.request_max_retries(context) == 7
    assert transport.base_retry_delay_s(context) == 0.5
    assert transport.retry_delay_seconds(0, context) >= 0.5
