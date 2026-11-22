from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_of

CORNER_FAILSAFE_CODE = "SESSION_ABORTED_BY_CORNER_FAILSAFE"
_MAX_IMAGE_BYTES = 24 * 1024 * 1024
_MAX_IMAGE_B64_CHARS = ((_MAX_IMAGE_BYTES + 2) // 3) * 4 + 4

_MACOS_KEYCODE_TO_WIN_NAME: dict[int, str] = {
    0: "a",
    1: "s",
    2: "d",
    3: "f",
    4: "h",
    5: "g",
    6: "z",
    7: "x",
    8: "c",
    9: "v",
    11: "b",
    12: "q",
    13: "w",
    14: "e",
    15: "r",
    16: "y",
    17: "t",
    18: "1",
    19: "2",
    20: "3",
    21: "4",
    22: "6",
    23: "5",
    24: "=",
    25: "9",
    26: "7",
    27: "-",
    28: "8",
    29: "0",
    30: "]",
    31: "o",
    32: "u",
    33: "[",
    34: "i",
    35: "p",
    36: "Enter",
    37: "l",
    38: "j",
    39: "'",
    40: "k",
    41: ";",
    42: "\\",
    43: ",",
    44: "/",
    45: "n",
    46: "m",
    47: ".",
    48: "Tab",
    49: "Space",
    50: "`",
    51: "Backspace",
    53: "Escape",
    55: "Win",
    56: "Shift",
    58: "Alt",
    59: "Ctrl",
    96: "f5",
    97: "f6",
    98: "f7",
    99: "f3",
    100: "f8",
    101: "f9",
    103: "f11",
    109: "f10",
    111: "f12",
    115: "Home",
    116: "PageUp",
    117: "Delete",
    118: "f4",
    119: "End",
    120: "f2",
    121: "PageDown",
    122: "f1",
    123: "Left",
    124: "Right",
    125: "Down",
    126: "Up",
}

_MODIFIER_MAP: dict[str, str] = {
    "ctrl": "Ctrl",
    "control": "Ctrl",
    "alt": "Alt",
    "shift": "Shift",
    "cmd": "Win",
    "win": "Win",
}


@dataclass(frozen=True)
class ParsedToolPayload:
    data: dict[str, object]
    image_bytes: bytes | None = None
    mime_type: str | None = None
    error_code: str | None = None
    error_message: str | None = None


def ssh_tunnel_argv(ssh_host: str, *, local_port: int, remote_port: int) -> list[str]:
    return [
        "ssh",
        "-N",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-L",
        f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}",
        "--",
        ssh_host,
    ]


def mime_to_extension(mime_type: str) -> str:
    normalized = mime_type.strip().lower().split(";", 1)[0]
    if normalized == "image/webp":
        return ".webp"
    if normalized in ("image/jpeg", "image/jpg"):
        return ".jpg"
    return ".png"


def macos_keycode_to_win_key(
    code: int, *, cmd: bool = False, modifiers: list[str] | None = None
) -> str | None:
    key_name = _MACOS_KEYCODE_TO_WIN_NAME.get(code)
    if key_name is None:
        return None
    parts: list[str] = []
    seen: set[str] = set()
    if cmd:
        parts.append("Win")
        seen.add("Win")
    for mod in modifiers or ():
        mapped = _MODIFIER_MAP.get(mod.strip().lower())
        if mapped and mapped not in seen:
            parts.append(mapped)
            seen.add(mapped)
    if key_name not in seen:
        parts.append(key_name)
    return "+".join(parts)


def parse_streamable_http_body(body: bytes, content_type: str) -> dict[str, object]:
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        raise ValueError("remote desktop helper returned an empty response body")
    ct = content_type.strip().lower()
    if "text/event-stream" in ct or text.startswith(("event:", "data:")):
        last_json: dict[str, object] | None = None
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload:
                continue
            try:
                parsed = object_of(json.loads, payload)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"remote desktop helper returned malformed SSE JSON: {exc}"
                ) from exc
            if is_object_dict(parsed):
                last_json = parsed
        if last_json is None:
            raise ValueError("remote desktop helper SSE stream contained no JSON-RPC payload")
        return last_json
    try:
        parsed = object_of(json.loads, text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"remote desktop helper returned malformed JSON: {exc}") from exc
    if not is_object_dict(parsed):
        raise ValueError("remote desktop helper response is not a JSON object")
    return parsed


def _decode_image_block(block: dict[str, object]) -> tuple[bytes | None, str | None, str | None]:
    raw_data = str(block["data"])
    if len(raw_data) > _MAX_IMAGE_B64_CHARS:
        return None, None, f"remote desktop helper image exceeds {_MAX_IMAGE_BYTES} byte limit"
    try:
        image_bytes = base64.b64decode(raw_data, validate=True)
    except (ValueError, binascii.Error) as exc:
        return None, None, f"remote desktop helper returned invalid image: {exc}"
    if len(image_bytes) > _MAX_IMAGE_BYTES:
        return None, None, f"remote desktop helper image exceeds {_MAX_IMAGE_BYTES} byte limit"
    mime = block.get("mime_type") or block.get("mimeType") or "image/png"
    return image_bytes, str(mime), None


def _collect_content_blocks(
    content: list[object],
) -> tuple[str, bytes | None, str | None, str | None]:
    text_chunks: list[str] = []
    image_bytes: bytes | None = None
    image_mime: str | None = None
    for block in content:
        if not is_object_dict(block):
            continue
        btype = block.get("type")
        if btype == "text" and isinstance(block.get("text"), str):
            text_chunks.append(str(block["text"]))
        elif btype == "image" and isinstance(block.get("data"), str):
            image_bytes, image_mime, img_err = _decode_image_block(block)
            if img_err is not None:
                return "", None, None, img_err
    return "\n".join(text_chunks).strip(), image_bytes, image_mime, None


def _decode_tool_json(
    joined: str, *, image_bytes: bytes | None, image_mime: str | None
) -> ParsedToolPayload:
    try:
        parsed = object_of(json.loads, joined)
    except json.JSONDecodeError:
        return ParsedToolPayload(
            data={}, error_message=f"remote desktop tool returned non-JSON text: {joined[:200]}"
        )
    if not is_object_dict(parsed):
        return ParsedToolPayload(data={}, error_message="remote desktop payload is not an object")
    if parsed.get("ok") is False:
        code_raw = str(parsed.get("code") or "")
        msg = str(parsed.get("message") or parsed.get("error") or "remote desktop tool failed")
        return ParsedToolPayload(data=parsed, error_code=code_raw or None, error_message=msg)
    return ParsedToolPayload(data=parsed, image_bytes=image_bytes, mime_type=image_mime)


def extract_mcp_tool_payload(rpc_response: Mapping[str, object]) -> ParsedToolPayload:
    err_obj = rpc_response.get("error")
    if is_object_dict(err_obj):
        return ParsedToolPayload(
            data={}, error_message=f"remote desktop RPC error: {err_obj.get('message') or err_obj}"
        )
    result = rpc_response.get("result")
    if not is_object_dict(result):
        return ParsedToolPayload(data={}, error_message="remote desktop RPC reply missing result")
    content = result.get("content")
    if not is_object_list(content):
        return ParsedToolPayload(
            data={}, error_message="remote desktop MCP tool reply missing content list"
        )
    joined, image_bytes, image_mime, img_err = _collect_content_blocks(content)
    if img_err is not None:
        return ParsedToolPayload(data={}, error_message=img_err)
    if result.get("isError") is True:
        code = CORNER_FAILSAFE_CODE if CORNER_FAILSAFE_CODE in joined else None
        return ParsedToolPayload(
            data={}, error_code=code, error_message=joined or "remote desktop tool error"
        )
    if not joined:
        return ParsedToolPayload(data={}, image_bytes=image_bytes, mime_type=image_mime)
    return _decode_tool_json(joined, image_bytes=image_bytes, image_mime=image_mime)


def map_ax_act_to_win_action(
    action: str,
) -> Literal["invoke", "set_value", "focus", "right_click", "click"]:
    if action == "press":
        return "invoke"
    if action == "set_value":
        return "set_value"
    if action == "focus":
        return "focus"
    if action == "show_menu":
        return "right_click"
    return "click"
