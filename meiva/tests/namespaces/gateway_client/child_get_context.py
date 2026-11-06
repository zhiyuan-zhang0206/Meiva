import httpx

from base.agents.context import MeivaContext
from base.agents.context.clients import ClientSet
from meiva.gateway_client import get
from meiva.sdk_surface import process_context


def main() -> None:
    for shared_host in (False, True):
        if shared_host:
            process_context.bind_host_process()
        try:
            get("/api/read")
        except process_context.ContextOutsideProcessError as exc:
            assert "context" in str(exc)
        else:
            raise AssertionError("ambient GET must refuse an unbound or shared host")

    def answer(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://explicit/api/read?q=host"
        assert request.extensions["timeout"]["read"] == 7
        return httpx.Response(200, json={"ok": True})

    context = MeivaContext(
        clients=ClientSet(
            gateway_url="http://explicit",
            gateway=lambda url: httpx.Client(
                base_url=url, timeout=7, transport=httpx.MockTransport(answer)
            ),
        )
    )
    try:
        assert get("/api/read", params={"q": "host"}, max_retries=1, context=context).json() == {
            "ok": True
        }
    finally:
        context.clients.close()


if __name__ == "__main__":
    main()
