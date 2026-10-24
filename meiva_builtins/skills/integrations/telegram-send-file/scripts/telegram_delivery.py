# meiva:note
# The decisions of the telegram-send-file skill, over values: which credentials the
# environment names, whether a file and caption may be sent, the sendDocument request,
# how its response reads, and the success line. Reading the file, the HTTP exchange and
# printing live in send_file.py. The bot token rides in the request URL, so nothing here
# puts a URL into an error.

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

# meiva:note
# Telegram's hard cap for documents sent through the Bot API.
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024

# meiva:note
# Telegram's caption cap for sendDocument (task #3696 exception inventory).
MAX_CAPTION_CHARS = 1024

# meiva:note
# Uploads up to 50 MB over a private link need more headroom than a text message; a slow
# link should fail loudly rather than hang the caller.
SEND_TIMEOUT_S = 120.0

TELEGRAM_API_BASE = "https://api.telegram.org"


# meiva:note
# The bot token and the owner chat id to deliver to. Zero or empty means not configured.
@dataclass(frozen=True)
class Credentials:
    token: str
    chat_id: int

    def require(self) -> None:
        if not self.token:
            raise RuntimeError("Telegram bot token not configured (MEIVA_TELEGRAM_BOT_TOKEN)")
        if not self.chat_id:
            raise RuntimeError("Telegram owner chat not configured (MEIVA_TELEGRAM_OWNER_ID)")


# meiva:note
# What to POST to sendDocument. The multipart file part is attached by the shell.
@dataclass(frozen=True)
class SendRequest:
    url: str
    chat_id: int
    caption: str | None

    def form(self) -> dict[str, str | int]:
        if self.caption is None:
            return {"chat_id": self.chat_id}
        return {"chat_id": self.chat_id, "caption": self.caption}


# meiva:note
# Parse the two env aliases from an environment mapping; a malformed owner id reads as 0.
def credentials_from_env(env: Mapping[str, str]) -> Credentials:
    token = env.get("MEIVA_TELEGRAM_BOT_TOKEN", "")
    owner_raw = env.get("MEIVA_TELEGRAM_OWNER_ID", "0")
    try:
        owner = int(owner_raw)
    except ValueError:
        owner = 0
    return Credentials(token=token, chat_id=owner)


# meiva:note
# Gates on a document's size: it must be non-empty and within Telegram's 50 MB cap. A file
# that fails them fails fast with a message the caller can show the user.
def check_document_size(path: str, size: int) -> None:
    if size <= 0:
        raise ValueError(f"empty file: {path}")
    if size > MAX_DOCUMENT_BYTES:
        raise ValueError(
            "file exceeds Telegram's sendDocument cap "
            f"({MAX_DOCUMENT_BYTES // (1024 * 1024)} MB): {path} ({size} bytes)"
        )


def check_caption(caption: str | None) -> None:
    if caption is not None and len(caption) > MAX_CAPTION_CHARS:
        raise ValueError(f"caption exceeds {MAX_CAPTION_CHARS} chars")


# meiva:note
# The sendDocument request for `credentials` at `api_base`; an over-long caption is refused.
def build_request(api_base: str, credentials: Credentials, caption: str | None) -> SendRequest:
    check_caption(caption)
    return SendRequest(
        url=f"{api_base}/bot{credentials.token}/sendDocument",
        chat_id=credentials.chat_id,
        caption=caption,
    )


def _json_object(value: object) -> Mapping[str, object] | None:
    return cast("Mapping[str, object]", value) if isinstance(value, dict) else None


# meiva:note
# The result object of a sendDocument response. A status other than 200 keeps the status
# and the first 200 characters of the body (which never contains the token); an
# `ok: false` body reports the API's description; a body without a result carrying a
# message id is refused rather than indexed.
def read_response(status_code: int, body: str) -> Mapping[str, object]:
    if status_code != 200:
        raise RuntimeError(f"telegram sendDocument failed: HTTP {status_code} - {body[:200]}")
    payload = _json_object(json.loads(body))
    if payload is None:
        raise RuntimeError("telegram sendDocument failed: API answered with a non-object body")
    if not payload.get("ok"):
        raise RuntimeError(
            f"telegram sendDocument failed: API {payload.get('description', 'unknown error')}"
        )
    result = _json_object(payload.get("result"))
    if result is None or "message_id" not in result:
        raise RuntimeError("telegram sendDocument failed: API result carries no message_id")
    return result


def success_line(filename: str, size: int, chat_id: int, result: Mapping[str, object]) -> str:
    return f"sent {filename} ({size} bytes) to chat {chat_id}: message_id={result['message_id']}"
