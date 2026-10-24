# meiva:note
# The harness the meiva.web tests share: a real HTTP listener on port 0 that answers the
# providers' requests, a scripted chat model for the answer step, and the SDK calls run against
# deps whose endpoints point at the listener. The deps come from the same `wiring` functions the
# SDK uses, over the test's configuration authority and catalog, so nothing is patched.

from __future__ import annotations

import dataclasses
import socket
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

import pytest
from pydantic import SecretStr

from base.config.service_read import ConfigAuthority
from base.lm.catalog import ModelCatalog
from base.lm.effort import ReasoningEffort
from base.packages.tests.model_catalog import inert_keys
from meiva.sdk_surface import process_context
from meiva.sdk_surface.batch import DEFAULT_BATCH_MAX_CONCURRENT
from meiva.sdk_surface.pure.media.finding_plan import FindingDelivery
from meiva.tests.fixtures.scripted_chat_model import OVERRIDE, ScriptedModels
from meiva.web import plans, runner, wiring
from meiva.web.deps import FetchDeps, ReaderProvider, SearchDeps, SearchProvider
from meiva.web.wire import Hit


@dataclass(frozen=True)
class Received:
    method: str
    target: str
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True)
class Reply:
    body: bytes
    status: int = 200


type Handler = Callable[[Received], Reply]


# meiva:note
# An HTTP listener on port 0 whose answer a test chooses and whose requests it can read.
class Loopback:
    def __init__(self) -> None:
        self.received: list[Received] = []
        self._handler: Handler = lambda _request: Reply(b"{}")
        loopback = self

        class _Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                request = Received(
                    method=self.command,
                    target=self.path,
                    headers={name.title(): value for name, value in self.headers.items()},
                    body=self.rfile.read(length) if length else b"",
                )
                loopback.received.append(request)
                reply = loopback._handler(request)
                self.send_response(reply.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply.body)))
                self.end_headers()
                self.wfile.write(reply.body)

            def do_GET(self) -> None:
                self._serve()

            def do_POST(self) -> None:
                self._serve()

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"

    def answer(self, body: bytes, status: int = 200) -> None:
        self._handler = lambda _request: Reply(body, status)

    def respond(self, handler: Handler) -> None:
        self._handler = handler

    def refused_url(self) -> str:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        return f"http://127.0.0.1:{port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def loopback() -> Iterator[Loopback]:
    server = Loopback()
    try:
        yield server
    finally:
        server.close()


# meiva:note
# `meiva.web` over deps whose providers all live on one loopback listener.
#
# `set_brave` / `set_jina` / `set_model` change the config values of the deps, `script` scripts
# the answer model the fetch deps name, and `search` / `fetch` run the SDK calls against them.
# Findings the calls raise land in the state update of the exec turn `use_exec_turn` binds, and
# `findings` reads them back from there.
@dataclass
class WebHarness:
    loopback: Loopback
    scripted_models: ScriptedModels
    authority: ConfigAuthority
    catalog: ModelCatalog
    search_deps: SearchDeps
    fetch_deps: FetchDeps

    def set_brave(self, **changes: Any) -> None:
        self.search_deps = dataclasses.replace(
            self.search_deps, brave=dataclasses.replace(self.search_deps.brave, **changes)
        )

    def set_provider(self, provider: SearchProvider) -> None:
        self.search_deps = dataclasses.replace(self.search_deps, provider=provider)

    def set_reader(self, reader: ReaderProvider) -> None:
        self.fetch_deps = dataclasses.replace(self.fetch_deps, reader=reader)

    def set_deepseek_search(self, **changes: Any) -> None:
        self.search_deps = dataclasses.replace(
            self.search_deps,
            deepseek=dataclasses.replace(self.search_deps.deepseek, **changes),
        )

    def set_deepseek_fetch(self, **changes: Any) -> None:
        self.fetch_deps = dataclasses.replace(
            self.fetch_deps,
            deepseek=dataclasses.replace(self.fetch_deps.deepseek, **changes),
        )

    def set_jina(self, **changes: Any) -> None:
        self.fetch_deps = dataclasses.replace(
            self.fetch_deps, jina=dataclasses.replace(self.fetch_deps.jina, **changes)
        )

    def set_model(self, **changes: Any) -> None:
        self.fetch_deps = dataclasses.replace(
            self.fetch_deps,
            fetch_model=dataclasses.replace(self.fetch_deps.fetch_model, **changes),
        )

    # meiva:note
    # Make a finding deliverable: the calls run inside an exec turn bound in this process (the
    # fixture unbinds it), and the injection check reads its settings from `authority` and its
    # decision model from `catalog`.
    def use_exec_turn(self, authority: ConfigAuthority, catalog: ModelCatalog) -> None:
        process_context.set_state(object())
        process_context.set_state_update({})
        self.fetch_deps = dataclasses.replace(
            self.fetch_deps,
            injection=wiring.injection_inputs(
                authority, catalog, keys=inert_keys(catalog), agent_id=None, delivery_active=True
            ),
        )

    # meiva:note
    # The findings delivered to the bound exec turn so far; none outside one.
    @property
    def findings(self) -> list[Any]:
        if not process_context.in_exec_turn():
            return []
        update = cast("dict[str, Any]", process_context.get_state_update())
        return list(update.get("security_findings", []))

    # meiva:note
    # What the facade reads before a call: scanning is enabled, and a turn is running or not.
    def delivery(self) -> FindingDelivery:
        return FindingDelivery(enabled=True, in_turn=process_context.in_exec_turn())

    # meiva:note
    # Script the answer model the fetch deps name.
    def script(self, *replies: Any):
        return self.scripted_models.script(self.fetch_deps.fetch_model.model, *replies)

    def search(
        self,
        queries: list[str],
        count: int = 10,
        max_concurrent: int | None = DEFAULT_BATCH_MAX_CONCURRENT,
    ) -> list[list[Hit]]:
        return runner.run_search(
            plans.plan_search(queries, count, max_concurrent), self.search_deps, self.delivery()
        )

    def fetch(
        self,
        targets: list[tuple[str, str]],
        max_chars: int = 50_000,
        effort: str | ReasoningEffort | None = None,
        max_concurrent: int | None = DEFAULT_BATCH_MAX_CONCURRENT,
    ) -> list[str]:
        return runner.run_fetch(
            plans.plan_fetch(targets, max_chars, effort, max_concurrent),
            self.fetch_deps,
            self.delivery(),
        )


# meiva:note
# The web deps of the test configuration, every provider pointed at the loopback listener.
#
# The calls run outside an exec turn, so a finding is dropped and the injection check is off;
# `use_exec_turn` turns them on.
@pytest.fixture
def web_harness(
    loopback: Loopback,
    scripted_models: ScriptedModels,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
) -> Iterator[WebHarness]:
    from base.config.tests.configuration import model_overrides

    search = wiring.search_deps_for(config_authority)
    fetch = wiring.fetch_deps_for(
        config_authority,
        catalog=model_catalog,
        keys=inert_keys(model_catalog),
        overrides=model_overrides(),
        delivery_active=False,
        agent_id=None,
    )
    search = dataclasses.replace(
        search,
        brave=dataclasses.replace(
            search.brave, api_key=SecretStr("fake-key"), endpoint=f"{loopback.url}/brave"
        ),
        deepseek=dataclasses.replace(search.deepseek, endpoint=f"{loopback.url}/deepseek/search"),
    )
    fetch = dataclasses.replace(
        fetch,
        jina=dataclasses.replace(fetch.jina, api_key=None, reader_base=f"{loopback.url}/jina/"),
        deepseek=dataclasses.replace(fetch.deepseek, endpoint=f"{loopback.url}/deepseek/fetch"),
        fetch_model=dataclasses.replace(fetch.fetch_model, llm_override=OVERRIDE),
    )
    harness = WebHarness(
        loopback=loopback,
        scripted_models=scripted_models,
        authority=config_authority,
        catalog=model_catalog,
        search_deps=search,
        fetch_deps=fetch,
    )
    yield harness
    process_context.unbind_exec_turn()


__all__ = ["Loopback", "Received", "Reply", "WebHarness", "loopback", "web_harness"]
