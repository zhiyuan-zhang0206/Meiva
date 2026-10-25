"""Canonical qualified provider identity; never text or time deduplication."""

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from services.entrypoints.im_bridge.ingress.types import ProviderSource


def uint64_message_id(provider_id: object) -> str | None:
    """Canonical positive iLink uint64, without float or boolean coercion."""
    if type(provider_id) is int:
        value = provider_id
    elif (
        isinstance(provider_id, str)
        and provider_id.isascii()
        and provider_id.isdigit()
        and len(provider_id) <= 20
    ):
        value = int(provider_id)
    else:
        return None
    return str(value) if 0 < value < 2**64 else None


def _split_bracketed_host_port(netloc: str) -> tuple[str, str | None] | None:
    end = netloc.find("]")
    if end <= 1:
        return None
    host = netloc[1:end]
    rest = netloc[end + 1 :]
    if not rest:
        return host, None
    if not rest.startswith(":"):
        return None
    return host, rest[1:]


def _parse_host_port(netloc: str) -> tuple[str, int | None] | None:
    if not netloc or "@" in netloc or any(ch.isspace() for ch in netloc):
        return None
    if netloc.startswith("["):
        split = _split_bracketed_host_port(netloc)
        if split is None:
            return None
        host, port_opt = split
        if port_opt is None:
            return host, None
        port_str = port_opt
    elif ":" in netloc:
        host, port_str = netloc.split(":", 1)
        if ":" in port_str:
            return None
    else:
        return netloc, None
    if not host or not port_str.isascii() or not port_str.isdigit():
        return None
    port = int(port_str)
    return (host, port) if 1 <= port <= 65535 else None


def provider_namespace(base_url: str) -> str | None:
    """Qualify the configured HTTPS provider endpoint without credential components."""
    if "://" not in base_url or "?" in base_url or "#" in base_url:
        return None
    scheme, remainder = base_url.split("://", 1)
    if scheme.lower() != "https":
        return None
    netloc, sep, path = remainder.partition("/")
    parsed = _parse_host_port(netloc)
    if parsed is None:
        return None
    host, port = parsed
    formatted_host = f"[{host}]" if ":" in host else host
    authority = formatted_host.lower() + (f":{port}" if port not in (None, 443) else "")
    normalized_path = ("/" + path).rstrip("/") if sep else ""
    return f"https://{authority}{normalized_path}"


def source_chat_key(source: ProviderSource) -> str:
    material = json.dumps(
        [source.namespace, source.account_id, source.sender_id, source.message_id]
    )
    return "weixin-chat-v1:" + hashlib.sha256(material.encode()).hexdigest()


def validate_message_items(items: Sequence[Mapping[str, object]]) -> None:
    """Known iLink item enums; unsupported known media remains explicit terminal content."""
    for item in items:
        kind = item.get("type")
        if type(kind) is not int or kind not in (0, 1, 2, 3, 4, 5, 11, 12):
            raise ValueError("Weixin provider item type is unknown")
        for field in ("text_item", "voice_item"):
            data = item.get(field)
            data_any: Any = data
            if data is not None:
                if not isinstance(data, Mapping):
                    raise TypeError("Weixin text/transcript must be an optional string")
                text_val: object = data_any.get("text")
                if text_val is not None and not isinstance(text_val, str):
                    raise TypeError("Weixin text/transcript must be an optional string")


def poll_result_codes(response: Mapping[str, object]) -> tuple[int, int | None]:
    ret, errcode = response.get("ret"), response.get("errcode")
    if type(ret) is not int or (errcode is not None and type(errcode) is not int):
        raise TypeError("Weixin provider response requires an integer ret and optional errcode")
    return ret, errcode


def poll_update_fields(
    response: Mapping[str, object], timeout_ms: int
) -> tuple[list[dict[str, object]], str | None, int]:
    suggested = response.get("longpolling_timeout_ms")
    if suggested is not None:
        if type(suggested) is not int or suggested <= 0:
            raise TypeError("Weixin long-poll timeout must be a positive integer")
        timeout_ms = suggested
    new_buf = response.get("get_updates_buf")
    if new_buf is not None and not isinstance(new_buf, str):
        raise TypeError("Weixin continuation token must be an opaque string")
    raw_msgs = response.get("msgs")
    msgs_any: Any = raw_msgs
    if not isinstance(raw_msgs, list):
        raise TypeError("Weixin updates must contain a message list")
    raw_items: list[Any] = msgs_any
    messages: list[dict[str, object]] = []
    for message in raw_items:
        msg_any: Any = message
        if not isinstance(message, dict):
            raise TypeError("Weixin provider message must be an object")
        item: dict[str, object] = msg_any
        messages.append(item)
    return messages, new_buf, timeout_ms
