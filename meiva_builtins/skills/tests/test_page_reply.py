# meiva:note
# The shipped page resources submit user input through the page-reply exit, not the whole
# API: the URL is absolute, the credentials are the viewer's own, and the body is the
# bounded envelope the gateway renders. Task #6058.

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

PAGES = Path(__file__).parents[3] / "meiva_builtins/skills/platform/meiva-guide/pages"
_GATEWAY = "http://gateway.test:8000"


# meiva:note
# Node executes the shipped browser resource; the frontend lane provisions it. One lookup
# site for every test here, so the host-platform record keeps a single entry for this file.
@pytest.fixture
def node() -> str:
    path = shutil.which("node")
    if path is None:
        pytest.skip("Node.js is required to execute the shipped browser resource")
    return path


def _reply_body(resource: str) -> str:
    if resource == "reply":
        return (PAGES / "widgets/meiva_reply/reply.js").read_text()
    html = (PAGES / f"widgets/{resource}/{resource}.html").read_text()
    start = html.index("  const MEIVA_AGENT_ID =")
    end = html.index("\n  // --- render", start)
    return html[start:end]


def _run(node: str, body: str, *, status: int) -> dict[str, Any]:
    harness = f"""
let request;
globalThis.fetch = async (url, options) => {{
  request = {{url, ...options}};
  return {{ok: {json.dumps(status < 400)}, status: {status}, json: async () => ({{inbound_id: 7}})}};
}};
{body}
let result;
let error;
try {{ result = await meivaReply("choice", ["B"], ""); }} catch (e) {{ error = e.message; }}
process.stdout.write(JSON.stringify({{request, result, error}}));
"""
    # meiva:note
    # Only shipped JavaScript and fixed fixture values reach the argv: no shell, no user input.
    run = subprocess.run(  # noqa: S603
        [node, "--input-type=module", "-e", harness], capture_output=True, text=True, check=True
    )
    return json.loads(run.stdout)


@pytest.mark.parametrize("resource", ["reply", "choice", "confirm", "form", "compare"])
@pytest.mark.parametrize("status", [201, 401])
def test_a_templated_origin_makes_the_reply_absolute(node: str, resource: str, status: int) -> None:
    body = (
        _reply_body(resource)
        .replace("__AGENT_ID__", "42")
        .replace("__PAGE_NAME__", "pick")
        .replace("__GATEWAY_ORIGIN__", _GATEWAY)
    )
    output = _run(node, body, status=status)
    request = output["request"]
    # meiva:note
    # Absolute, so the page keeps working when it moves off the gateway origin (task #6058);
    # credentials include, so the gateway still sees the viewer's session.
    assert request["url"] == f"{_GATEWAY}/pages/42-pick/reply"
    assert request["credentials"] == "include"
    assert request["method"] == "POST"
    assert request["headers"] == {"Content-Type": "application/json"}
    assert json.loads(request["body"]) == {"kind": "choice", "choices": ["B"], "text": ""}
    if status < 400:
        assert output["result"] == {"inbound_id": 7}
        assert "error" not in output
    else:
        assert output["error"] == "meivaReply failed: HTTP 401"
        assert "result" not in output


def test_an_unreplaced_origin_falls_back_to_the_page_own_origin(node: str) -> None:
    # meiva:note
    # Today's pages are served from the gateway, so a template that was never filled in keeps
    # working; that is the single value that changes when pages move.
    body = _reply_body("reply").replace("__AGENT_ID__", "42").replace("__PAGE_NAME__", "pick")
    output = _run(
        node, "globalThis.location = { origin: 'http://page.test' };\n" + body, status=201
    )
    assert output["request"]["url"] == "http://page.test/pages/42-pick/reply"
