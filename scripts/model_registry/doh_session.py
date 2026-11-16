# meiva:note
# Pinned-IP HTTPS transport and DNS-over-HTTPS fallback for fake-IP (`198.18.0.0/15`) resolvers.

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from typing import override

import requests
from requests.adapters import HTTPAdapter
from urllib3 import _base_connection
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

from base.host.boundary.json_values import is_object_dict, is_object_list
from scripts.model_registry.fetch_ports import Addrinfo, HttpGet

_DOH_TIMEOUT_SECONDS = 10
DOH_ENDPOINTS = ("https://cloudflare-dns.com/dns-query", "https://dns.google/resolve")
_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
doh_cache: dict[str, str] = {}


def response_object(value: object, name: str) -> dict[str, object]:
    if not is_object_dict(value):
        raise TypeError(f"{name} response must be a JSON object")
    return value


def is_fake_ip(address: str) -> bool:
    try:
        return ipaddress.ip_address(address) in _FAKE_IP_NETWORK
    except ValueError:
        return False


# meiva:note
# Keep the origin host independent from urllib3's DNS target.
class _PinnedIPHTTPSConnection(HTTPSConnection):
    _origin_host: str

    @property
    def host(self) -> str:
        return self._origin_host.rstrip(".")

    @host.setter
    def host(self, value: str) -> None:
        self._origin_host = value
        self._dns_host = value


def _pin_connection(connection: object, pinned_ip: str) -> None:
    if not isinstance(connection, HTTPSConnection):
        raise TypeError(f"Expected HTTPSConnection, got {type(connection).__name__}")
    origin_host = connection.host
    connection.__class__ = _PinnedIPHTTPSConnection
    if not isinstance(connection, _PinnedIPHTTPSConnection):
        raise TypeError(f"Expected _PinnedIPHTTPSConnection, got {type(connection).__name__}")
    connection._origin_host = origin_host
    connection._dns_host = pinned_ip


# meiva:note
# Create connections that dial a pinned IP while retaining the origin host.
class _PinnedIPConnectionPool(HTTPSConnectionPool):
    pinned_ip = ""

    @override
    def _new_conn(self) -> _base_connection.BaseHTTPSConnection:
        connection = super()._new_conn()
        _pin_connection(connection, self.pinned_ip)
        return connection


# meiva:note
# Dial the pinned IP while SNI, Host, and certificate checks use the real hostname.
class PinnedIPHTTPSAdapter(HTTPAdapter):
    def __init__(self, pinned_ip: str) -> None:
        self._pinned_ip = pinned_ip
        super().__init__()
        pinned_pool = type(
            "_PinnedIPConnectionPool", (_PinnedIPConnectionPool,), {"pinned_ip": self._pinned_ip}
        )
        self.poolmanager.pool_classes_by_scheme = {
            "http": HTTPConnectionPool,
            "https": pinned_pool,
        }


def _first_real_doh_address(value: object, endpoint: str, host: str) -> str:
    payload = response_object(value, endpoint)
    answers = payload["Answer"]
    if not is_object_list(answers):
        raise TypeError(f"{endpoint} Answer must be a list")
    for answer in answers:
        if not is_object_dict(answer):
            raise TypeError(f"{endpoint} Answer entries must be objects")
        address = answer.get("data")
        if answer.get("type") in (1, "1") and isinstance(address, str) and not is_fake_ip(address):
            return address
    raise ValueError(f"{endpoint} returned no usable A answer for {host}")


def doh_real_ip(host: str, *, http_get: HttpGet) -> str:
    if host in doh_cache:
        return doh_cache[host]
    last_error: Exception | None = None
    for endpoint in DOH_ENDPOINTS:
        try:
            response = http_get(
                endpoint,
                headers={"accept": "application/dns-json"},
                params={"name": host, "type": "A"},
                timeout=_DOH_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            address = _first_real_doh_address(response.json(), endpoint, host)
            doh_cache[host] = address
            return address
        except Exception as exc:
            last_error = exc
    raise ConnectionError(f"DoH resolution failed for {host}") from last_error


def session_for_host(
    host: str,
    *,
    addrinfo: Addrinfo,
    doh_resolver: Callable[[str], str],
) -> requests.Session | None:
    try:
        address_rows = addrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return None
    ipv4_addresses = [
        socket_address[0]
        for family, _st, _proto, _canon, socket_address in address_rows
        if family == socket.AF_INET and isinstance(socket_address[0], str)
    ]
    if not any(is_fake_ip(address) for address in ipv4_addresses):
        return None
    # meiva:note
    # A detected fake IP must not fall back to the hijacked resolver if DoH fails.
    pinned_ip = doh_resolver(host)
    session = requests.Session()
    session.mount(f"https://{host}/", PinnedIPHTTPSAdapter(pinned_ip))
    return session
