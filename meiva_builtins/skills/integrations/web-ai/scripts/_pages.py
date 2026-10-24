# meiva:note
# What the web-ai driver makes of the text the chrome MCP hands back: the JSON block of an evaluated script, the selected tab of a page list, a clickable control of the rendered accessibility tree, a sign-in wall in a snapshot, and the verdicts of the inject and submit scripts. All of it is parsing and judging over strings and values; the calls that produced the text live in the driver.
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

_JSON_OPEN = "```json"
_SELECTED_RE = re.compile(r"^\s*(\d+):.*\[selected\]\s*$", re.MULTILINE)

# meiva:note
# The roles a click target may carry in the rendered accessibility tree.
_CLICKABLE_ROLES = "button|menuitem|menuitemradio|menuitemcheckbox|link|radio|option|tab"
_SNAPSHOT_NODE_RE = re.compile(rf'uid=(\S+)\s+(?:{_CLICKABLE_ROLES})\b[^"\n]*"([^"]+)"')


# meiva:note
# A control of the accessibility tree: the uid the chrome MCP clicks and the accessible name it was matched by.
@dataclass(frozen=True)
class Clickable:
    uid: str
    label: str


# meiva:note
# The JSON an evaluated script returned. chrome-devtools-mcp wraps the return value in a fenced json block, so this anchors on the FIRST opener and the LAST closing fence: backticks inside the returned content (code answers) must not truncate the JSON. Raises when there is no fenced block (the script threw, or no page is selected).
def parse_evaluate_response(response: str) -> object:
    start = response.find(_JSON_OPEN)
    end = response.rfind("```")
    if start < 0 or end <= start + len(_JSON_OPEN):
        raise RuntimeError(f"evaluate_script returned no JSON block: {response[:300]!r}")
    return json.loads(response[start + len(_JSON_OPEN) : end])


# meiva:note
# The numeric page id of the `[selected]` tab of a page-list dump, or None. new_page and list_pages render `  <id>: <url> [selected]` for the active tab and that id is what closing the tab feeds back. None means no line is marked (format drift); callers treat that as "nothing safe to close" and leave tabs be.
def selected_page_id(listing: str) -> int | None:
    found = _SELECTED_RE.search(listing)
    return int(found.group(1)) if found else None


# meiva:note
# The first control of the snapshot whose accessible name matches one of `texts`, case-insensitively. An exact pass over EVERY text runs before any substring pass, so an early broad text cannot steal the match from a later text's exact hit ("Start" grabbing the "Start dictation" mic button while "Start research" sits right there). The substring pass requires a short label, which guards against a huge container that merely contains the word.
def find_clickable(snapshot: str, texts: Sequence[str]) -> Clickable | None:
    nodes = [Clickable(uid, label.strip()) for uid, label in _SNAPSHOT_NODE_RE.findall(snapshot)]
    wants = [text.lower() for text in texts]
    exact = next((n for w in wants for n in nodes if n.label.lower() == w), None)
    if exact is not None:
        return exact
    return next(
        (n for w in wants for n in nodes if w in n.label.lower() and len(n.label) < 60),
        None,
    )


# meiva:note
# True when the snapshot shows any of the site's sign-in texts, case-insensitively.
def is_login_wall(snapshot: str, indicators: Sequence[str]) -> bool:
    lowered = snapshot.lower()
    return any(indicator.lower() in lowered for indicator in indicators)


# meiva:note
# Raise unless the inject script reported that the prompt landed: the composer was found and read back non-empty. An empty read-back means the editor rejected the programmatic insert, or the composer selector drifted.
def check_inject_result(label: str, result: Mapping[str, object]) -> None:
    if not result.get("ok"):
        raise RuntimeError(f"{label}: failed to inject prompt ({result.get('reason')})")
    if not result.get("got_len"):
        raise RuntimeError(
            f"{label}: prompt did not register in the composer "
            f"— the editor rejected the programmatic insert, or the composer selector drifted"
        )


# meiva:note
# The tag of the path the submit script fired (`button:<selector>` or `enter`), or a RuntimeError when it found neither target.
def submit_via(label: str, result: Mapping[str, object]) -> str:
    if not result.get("ok"):
        raise RuntimeError(f"{label}: could not submit ({result.get('reason')})")
    return str(result["via"])
