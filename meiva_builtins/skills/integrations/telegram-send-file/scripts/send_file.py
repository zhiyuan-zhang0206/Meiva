#!/usr/bin/env python3
"""Send one local file to the user's Telegram chat via the Bot API.

The only direct Telegram Bot API call an agent may make (user ruling
2026-08-13): a one-way ``sendDocument`` to the configured owner chat. Reading
updates, sending text, or any other Bot API method stays forbidden — text
delivery goes through IM Bridge.

Run from the Meiva source root with the venv Python:

    .venv/bin/python meiva_builtins/skills/integrations/telegram-send-file/scripts/send_file.py <path> [--caption TEXT]

Credentials come from the shared cluster config (``settings.telegram.*``,
env ``MEIVA_TELEGRAM_BOT_TOKEN`` / ``MEIVA_TELEGRAM_OWNER_ID``) — the same source
IM Bridge uses. Nothing is hardcoded, and the token never appears in output:
httpx exceptions embed the request URL, which carries the token, so every
error path is sanitized before it reaches the caller (the same discipline as
the im_bridge telegram adapter).

Exit code 0 with a one-line summary on success; 1 with ``error: <reason>``
on stderr on failure.
"""

from __future__ import annotations

import argparse
import mimetypes
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import httpx

from base.config import settings

# meiva:note
# `telegram_delivery` is a sibling in this skill's own scripts/ dir, not an importable
# package (PYTHONSAFEPATH=1 keeps a script's own directory off sys.path). Structure Rule 6
# recognizes this exact __file__-derived, within-skill shape as the endorsed guard.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from telegram_delivery import (
    SEND_TIMEOUT_S,
    TELEGRAM_API_BASE,
    Credentials,
    build_request,
    check_caption,
    check_document_size,
    credentials_from_env,
    read_response,
    success_line,
)


# meiva:note
# Return ``path`` as a resolved regular file, or raise. Gates: exists, regular file,
# non-empty, within Telegram's 50 MB document cap. A directory, a missing path, an empty
# file, or an oversized file all fail fast with a message the caller can show the user.
def validate_file(path: str | Path) -> Path:
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"not a file: {p}")
    check_document_size(str(p), p.stat().st_size)
    return p


# meiva:note
# POST one file to Telegram's sendDocument at `api_base`; return the result object. Errors
# are sanitized: httpx exceptions can embed the request URL, which contains the bot token,
# so only the exception class name is surfaced.
def send_document(
    *, api_base: str, credentials: Credentials, path: Path, caption: str | None = None
) -> Mapping[str, object]:
    request = build_request(api_base, credentials, caption)
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    try:
        with httpx.Client(timeout=SEND_TIMEOUT_S) as http, path.open("rb") as fh:
            resp = http.post(
                request.url,
                data=request.form(),
                files={"document": (path.name, fh, content_type)},
            )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"telegram sendDocument failed: {type(exc).__name__}") from None
    return read_response(resp.status_code, resp.text)


# meiva:note
# Validate and send; return the one-line success report. Every failure raises
# (FileNotFoundError / ValueError / RuntimeError) so `main` can render one uniform
# ``error: <reason>`` line.
def _deliver(path_arg: str, caption: str | None, *, api_base: str, credentials: Credentials) -> str:
    path = validate_file(path_arg)
    check_caption(caption)
    credentials.require()
    result = send_document(api_base=api_base, credentials=credentials, path=path, caption=caption)
    return success_line(path.name, path.stat().st_size, credentials.chat_id, result)


# meiva:note
# CLI entry point: validate, send, report. Returns the exit code.
def main(argv: Sequence[str], *, api_base: str, credentials: Credentials) -> int:
    parser = argparse.ArgumentParser(
        prog="telegram-send-file",
        description="Send one local file to the user's Telegram chat (Bot API sendDocument).",
    )
    parser.add_argument("path", help="local file path to deliver")
    parser.add_argument(
        "--caption",
        default=None,
        help="optional plain-text caption (at most 1024 chars)",
    )
    args = parser.parse_args(argv)
    try:
        print(_deliver(args.path, args.caption, api_base=api_base, credentials=credentials))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


# meiva:note
# The process entry reads the world once. Bot token + owner chat id come from the canonical env
# aliases (`load_meiva_env` populates them from $MEIVA_HOME/.env, the same source im_bridge's
# settings read), because agent processes may NOT construct the ``telegram`` settings domain
# (per-process config, Task #856: the agent profile excludes it, and the consumption-matrix
# guard keeps it that way; the guard recognizes a settings read only inside an
# ``if settings.has_domain("telegram")`` body). When the token alias is empty (a bare CLI
# context), the settings domain supplies both values if this process may construct it.
if __name__ == "__main__":
    credentials = credentials_from_env(os.environ)
    if settings.has_domain("telegram"):
        credentials = (
            Credentials(
                token=settings.telegram.telegram_bot_token,
                chat_id=settings.telegram.telegram_owner_id,
            )
            if credentials.lacks_token()
            else credentials
        )
    raise SystemExit(main(sys.argv[1:], api_base=TELEGRAM_API_BASE, credentials=credentials))
