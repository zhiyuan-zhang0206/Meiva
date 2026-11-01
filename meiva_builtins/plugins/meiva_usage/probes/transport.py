# meiva:note
# Transport protocol and error types for usage metric probes.
# meiva:link meiva_builtins/plugins/meiva_usage/docs/meiva_usage.meiva.okf.md

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field


class HttpStatusError(Exception):
    def __init__(self, status_code: int, message: str = "") -> None:
        super().__init__(f"HTTP {status_code}: {message}" if message else f"HTTP {status_code}")
        self.status_code = status_code
        self.message = message


@dataclass(frozen=True)
class HttpJsonRequest:
    url: str
    method: str = "GET"
    headers: Mapping[str, str] = field(default_factory=dict[str, str])
    body: bytes | None = None
    timeout_s: float = 10.0


HttpJsonFetcher = Callable[[HttpJsonRequest], Mapping[str, object]]
