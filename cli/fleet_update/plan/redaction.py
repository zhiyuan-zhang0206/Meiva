# meiva:note
# What the window's console and log never carry: a bearer credential, a value named like a
# secret, a password inside a URL. The hosts' own output goes through here line by line before
# it is printed or written, so a command that echoes its environment cannot leak it into the log
# an operator pastes into a ticket.
# meiva:link cli/docs/fleet-update.meiva.okf.md

from __future__ import annotations

import re

_NAME = r"\w*(?:secret|password|token|api_key|capability_key)\w*"


def redact(line: str) -> str:
    line = re.sub(r"(?i)(\bbearer\s+)[^\s\"',;]+", r"\1<redacted>", line)
    line = re.sub(rf"(?i)(\b{_NAME}[\"']?\s*[=:]\s*[\"']?)[^\s\"',;}}]+", r"\1<redacted>", line)
    return re.sub(r"(?i)([a-z][a-z0-9+.-]*://[^/\s:@]*:)[^@\s/]+@", r"\1<redacted>@", line)
