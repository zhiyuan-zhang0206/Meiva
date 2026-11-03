"""`UrlOpener`: the port for an HTTP request whose answer a test must control.

A component that makes an HTTP request takes `open_url: UrlOpener` as a required argument (no
default) and calls it instead of `urllib.request.urlopen`. The composition root passes
`SYSTEM_URL_OPENER`; a test passes `FakeUrlOpener` from `base/host/net/tests/fake_url_opener.py`,
which answers by URL prefix and refuses a URL nobody scripted, or it starts a real listener on
port 0 and uses the system opener. A test never patches `urllib.request.urlopen` or `socket`.

The shape is `urlopen`'s own: a request (or a URL) and a timeout in, a readable context manager
out. As with `urlopen`, a status outside 2xx raises `urllib.error.HTTPError`.
"""

from __future__ import annotations

import urllib.request
from collections.abc import Callable
from contextlib import AbstractContextManager
from http.client import HTTPResponse
from typing import Protocol

from base.host.boundary.untyped import object_of


# meiva:note
# What a caller of a `UrlOpener` reads of the answer: the body. `urlopen` types its answer `Any`;
# a test's fake only has to provide what the code under test reads, so the view is split by what a
# caller needs rather than one wide response.
class UrlResponse(Protocol):
    def read(self) -> bytes: ...


# meiva:note
# One header lookup: the name in, its value or None out (`HTTPMessage.get` and `dict.get` both fit).
class HeaderLookup(Protocol):
    def get(self, name: str, /) -> str | None: ...


# meiva:note
# The answer of a download that streams it: the headers (for `Content-Length`) and `read1`, which
# returns what has arrived without waiting to fill the buffer.
class StreamingUrlResponse(Protocol):
    @property
    def headers(self) -> HeaderLookup: ...

    def read1(self, amount: int = -1, /) -> bytes: ...


type UrlOpener = Callable[
    [str | urllib.request.Request, float], AbstractContextManager[UrlResponse]
]
type StreamingUrlOpener = Callable[
    [str | urllib.request.Request, float], AbstractContextManager[StreamingUrlResponse]
]
type SystemUrlOpener = Callable[
    [str | urllib.request.Request, float], AbstractContextManager[HTTPResponse]
]


# meiva:note
# `urlopen` for an http(s) URL, which answers an `HTTPResponse`. Any other scheme answers a different
# class (`file:` and `data:` an `addinfourl`) that this port does not serve, so it is refused here
# instead of reaching a caller that reads `headers` and `read1`. As with `urlopen`, each caller owns the
# validation of its URL.
def _urlopen(
    target: str | urllib.request.Request, timeout: float
) -> AbstractContextManager[HTTPResponse]:
    opened = object_of(urllib.request.urlopen, target, timeout=timeout)
    if not isinstance(opened, HTTPResponse):
        raise TypeError(f"urlopen answered {type(opened).__name__}, not an http(s) response")
    return opened


SYSTEM_URL_OPENER: SystemUrlOpener = _urlopen
