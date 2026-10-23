# meiva:note
# What a Claude Code launch can tell from the screen it captures and the markers its launch command leaves: whether the composer is up, whether the executable was missing, whether the CLI is signed out, and how a first message is wrapped for the composer. The launcher captures and waits; these decide what a capture means.
# meiva:link meiva/shell/coding_tools/docs/coding-tools.meiva.okf.md
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

# meiva:note
# One Claude composer chunk is ~1022 chars: a longer raw burst folds into collapsed paste fragments, and a submission made while such a fragment sits next to a raw tail drops the fragment's content (#4364).
PASTE_WRAP_THRESHOLD_CHARS = 1022

PASTE_BEGIN = "\x1b[200~"
PASTE_END = "\x1b[201~"

# meiva:note
# Typed after a pasted bootstrap. Claude Code marks a bracketed paste as pasted content, and the model will not act on instructions that arrive only inside a paste; it asks for confirmation instead, stalling an unattended session (observed on Claude Code 2.1.283, 2026-09-27). One line of the operator's own words, outside the paste, is the request to act on it.
OWN_WORDS = " Follow the launch message pasted above: it is from the Meiva agent that launched you."

CLAUDE_NOT_FOUND = "claude executable not found in PATH or $HOME/.local/bin/claude"

_NOT_FOUND_LINE = re.compile(
    r"(?m)^error: claude executable not found in PATH or \$HOME/\.local/bin/claude\r?$"
)

Verdict = Literal["missing", "logged_out", "exited", "ready", "waiting"]


# meiva:note
# Wrap a multi-chunk payload so the composer takes it as one atomic paste. The composer folds a raw burst longer than one chunk into collapsed paste fragments; when Enter lands with a collapsed fragment plus a raw tail in the composer, the fragment's content is dropped from the submission (#4364). Bracketed-paste markers make the composer take the whole payload as one paste whose content survives intact. Inner markers are stripped so the payload cannot close the wrapper early; a payload within one chunk is sent unchanged.
def bracketed_paste(text: str) -> str:
    body = text.replace(PASTE_BEGIN, "").replace(PASTE_END, "")
    if len(body) <= PASTE_WRAP_THRESHOLD_CHARS:
        return body
    return f"{PASTE_BEGIN}{body}{PASTE_END}"


# meiva:note
# What is typed for a bootstrap message. A pasted bootstrap is followed by `OWN_WORDS` before the Enter; a bootstrap within one composer chunk is typed as-is and needs none.
def paste_payload(message: str) -> str:
    payload = bracketed_paste(message)
    if payload.startswith(PASTE_BEGIN):
        payload += OWN_WORDS
    return payload


# meiva:note
# Require the title and a composer cue, normalizing Unicode prompt spacing. An empty composer is a bare prompt glyph: captures strip trailing blanks, so the line may end right after it. A resumed session opens on its conversation history instead of the welcome banner, so the title is not required there; the composer cue (with its bypass-permissions footer) still is, which a shell prompt never shows.
def claude_ui_ready(output: str, *, resumed: bool = False) -> bool:
    normalized = re.sub(r"[^\S\r\n]", " ", output)
    composer = "? for shortcuts" in normalized or (
        bool(re.search(r"(?m)^ *\u276f(?: |\r?$)", normalized))
        and "bypass permissions on" in normalized
    )
    return composer and (resumed or bool(re.search(r"\bClaude\s+Code\b", normalized)))


# meiva:note
# Written by the launch command when `claude auth status` reports signed out, beside the missing-executable marker.
def login_marker(failure_marker: Path) -> Path:
    return failure_marker.with_name("not-logged-in")


# meiva:note
# What one look at the session means. `screen` is the visible screen, None when the session has ended. A launched shell has a private marker, and its echoed command can wrap into arbitrary screen lines, so its screen text is never failure evidence; without a marker the executable-missing line on the screen is the evidence. `missing_marker` and `login_marker` are None when the launch made no marker.
def screen_verdict(
    screen: str | None,
    *,
    resumed: bool,
    missing_marker: bool | None,
    login_marker: bool | None,
) -> Verdict:
    if missing_marker:
        return "missing"
    if screen is None:
        return "exited"
    if missing_marker is None and _NOT_FOUND_LINE.search(screen):
        return "missing"
    if login_marker:
        return "logged_out"
    return "ready" if claude_ui_ready(screen, resumed=resumed) else "waiting"


# meiva:note
# The error a failed verdict raises. `settled` is whether the screen had already looked ready once, which changes how an ended session is described.
def failure_text(verdict: Verdict, session_id: int, *, settled: bool) -> str:
    if verdict == "missing":
        return f"Claude did not start: {CLAUDE_NOT_FOUND}"
    if verdict == "logged_out":
        return (
            "Claude Code is not logged in on this host; run `claude auth login` as the host "
            "user, then launch again. No text was delivered"
        )
    if settled:
        return f"Claude session {session_id} exited before it became ready"
    return (
        f"Claude session {session_id} exited before its UI appeared; check the executable "
        "in PATH or $HOME/.local/bin/claude"
    )
