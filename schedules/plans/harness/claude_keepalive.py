# meiva:note
# Pure decisions for the daily Claude Code login keepalive: the exact command, how to read the
# login status, and what one round reports. The round itself (locating the binary, running
# processes, printing, emitting) lives in `schedules/jobs/harness/claude_auth_keepalive.py`.
#
# The keepalive only runs the CLI. Claude Code refreshes its own access token and writes the rotated
# credentials back; the refresh token is single-use, so nothing here may call the OAuth refresh
# endpoint or touch a credentials file.
# meiva:link schedules/README.md

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, TypeGuard

from base.host.boundary.untyped import object_of

__all__ = [
    "KEEPALIVE_MODEL",
    "KEEPALIVE_PROMPT",
    "KEEPALIVE_TIMEOUT_S",
    "STATUS_TIMEOUT_S",
    "TIMEOUT_EXIT_CODE",
    "KeepaliveDecision",
    "WithoutRun",
    "decide_run",
    "decide_without_run",
    "keepalive_argv",
    "logged_in",
    "login_status_argv",
]

# meiva:note
# The cheapest model, a fixed one-line prompt, no tools: the round has to cost almost nothing and
# finish in a single turn, because its only purpose is the CLI's own token refresh.
KEEPALIVE_MODEL = "haiku"
KEEPALIVE_PROMPT = "Reply with the single word: ok"
KEEPALIVE_TIMEOUT_S = 120.0
STATUS_TIMEOUT_S = 30.0

# meiva:note
# What the process runner reports for a command that outlived its timeout.
TIMEOUT_EXIT_CODE = 124

_TAIL_LINES = 5
_TAIL_CHARS = 800

# meiva:note
# Token shapes that must never reach a log: `sk-` keys, bearer headers, JWTs, and
# `accessToken` / `refresh_token` style key-value pairs (the key is kept, the value masked).
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "***"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-~+/=]+"), "Bearer ***"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*"), "***"),
    (
        re.compile(r"""(?i)((?:access|refresh)[_\-]?token["']?\s*[:=]\s*["']?)[^\s"',}]+"""),
        r"\1***",
    ),
)

Status = Literal["ok", "failed", "skipped"]

# meiva:note
# The reasons a round ends without having run the keepalive command.
WithoutRun = Literal["non_production", "claude_not_found", "not_logged_in"]


@dataclass(frozen=True)
class KeepaliveDecision:
    status: Status
    reason: str
    log_line: str
    telemetry_payload: Mapping[str, object]


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


# meiva:note
# The prompt goes in on stdin: `--tools` takes a variable number of values and would swallow a
# positional prompt placed after it.
def keepalive_argv(claude_path: str) -> tuple[str, ...]:
    return (
        claude_path,
        "-p",
        "--model",
        KEEPALIVE_MODEL,
        "--tools",
        "",
        "--no-session-persistence",
        "--output-format",
        "text",
    )


def login_status_argv(claude_path: str) -> tuple[str, ...]:
    return (claude_path, "auth", "status", "--json")


# meiva:note
# `True`/`False` from the status command's `loggedIn` field, `None` when the output says neither
# (an unparsable or differently shaped answer): an unreadable status is not a verdict, and the
# round then lets the keepalive command itself find out.
def logged_in(status_stdout: str) -> bool | None:
    try:
        parsed = object_of(json.loads, status_stdout)
    except ValueError:
        return None
    if not _is_object_dict(parsed):
        return None
    flag = parsed.get("loggedIn")
    return flag if isinstance(flag, bool) else None


# meiva:note
# The last lines of a process's output with credential-shaped text masked: enough to say why a
# command failed, bounded so a runaway stderr cannot become the log.
def _output_tail(text: str) -> str:
    masked = text
    for pattern, replacement in _SECRET_PATTERNS:
        masked = pattern.sub(replacement, masked)
    lines = [line.rstrip() for line in masked.strip().splitlines() if line.strip()]
    tail = " | ".join(lines[-_TAIL_LINES:])
    return tail[-_TAIL_CHARS:]


_WITHOUT_RUN: dict[WithoutRun, tuple[Status, str]] = {
    "non_production": ("skipped", "skipped: non-production identity"),
    "claude_not_found": (
        "failed",
        "failed: the claude executable was not found on this machine; nothing to keep alive",
    ),
    "not_logged_in": (
        "failed",
        "failed: claude reports it is not logged in on this machine; run `claude auth login`",
    ),
}


def decide_without_run(reason: WithoutRun) -> KeepaliveDecision:
    status, log_line = _WITHOUT_RUN[reason]
    return KeepaliveDecision(
        status=status,
        reason=reason,
        log_line=log_line,
        telemetry_payload={"status": status, "reason": reason},
    )


def decide_run(exit_code: int, stdout: str, stderr: str) -> KeepaliveDecision:
    if exit_code == 0:
        return KeepaliveDecision(
            status="ok",
            reason="",
            log_line="ok: claude ran once and refreshed its own login if it was due",
            telemetry_payload={"status": "ok", "exit_code": 0},
        )
    reason = "timeout" if exit_code == TIMEOUT_EXIT_CODE else "nonzero_exit"
    detail = _output_tail(stderr) or _output_tail(stdout)
    return KeepaliveDecision(
        status="failed",
        reason=reason,
        log_line=f"failed: {reason} (exit code {exit_code}); output tail: {detail or '(none)'}",
        telemetry_payload={"status": "failed", "reason": reason, "exit_code": exit_code},
    )
