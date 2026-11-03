# meiva:note
# The HTTP and resolver seams of the model-update tracker, typed by what the tracker calls: a GET
# that answers a response with `raise_for_status` and a JSON body, a session whose `get` is such a
# GET, the resolver's `getaddrinfo`, and the page fetcher the provider readers share. `requests`
# satisfies the first two structurally and the tests pass fakes; none of them names `Any`.

from __future__ import annotations

import socket
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

__all__ = ["Addrinfo", "HttpGet", "HttpResponse", "HttpSession", "JsonFetcher", "SessionForHost"]


class HttpResponse(Protocol):
    def raise_for_status(self) -> None: ...

    def json(self) -> object: ...


class HttpGet(Protocol):
    def __call__(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str | int],
        timeout: float,
    ) -> HttpResponse: ...


class HttpSession(Protocol):
    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str | int],
        timeout: float,
    ) -> HttpResponse: ...


type SessionForHost = Callable[[str], HttpSession | None]


class Addrinfo(Protocol):
    def __call__(
        self, host: str, port: int, /, *, type: int
    ) -> Sequence[tuple[socket.AddressFamily, socket.SocketKind, int, str, tuple[object, ...]]]: ...


class JsonFetcher(Protocol):
    def __call__(
        self, url: str, *, headers: Mapping[str, str], params: Mapping[str, str | int]
    ) -> dict[str, object]: ...
