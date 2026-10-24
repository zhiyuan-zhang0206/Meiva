from __future__ import annotations

import json

from ops.private_files.model import Outcome
from ops.private_files.report import exit_code, render

_PASS = Outcome(entry_id="a", path="ops/a.py", ok=True, errors=())
_FAIL = Outcome(entry_id="b", path="ops/b.py", ok=False, errors=("file is missing", "other"))


# meiva:note
# The text form is one `OK`/`FAIL` line per entry, a failure listing its errors on that line.
def test_text_output_has_one_line_per_outcome() -> None:
    assert render([_PASS, _FAIL], json_output=False) == (
        "OK a ops/a.py\nFAIL b ops/b.py: file is missing; other\n"
    )


# meiva:note
# The JSON form is one array of the outcomes' machine-readable records.
def test_json_output_is_one_array() -> None:
    text = render([_PASS, _FAIL], json_output=True)

    assert text.endswith("\n")
    assert json.loads(text) == [
        {"id": "a", "path": "ops/a.py", "ok": True, "errors": []},
        {"id": "b", "path": "ops/b.py", "ok": False, "errors": ["file is missing", "other"]},
    ]


# meiva:note
# An empty registry prints nothing in text, and `[]` in JSON.
def test_an_empty_registry_renders_empty() -> None:
    assert render([], json_output=False) == ""
    assert json.loads(render([], json_output=True)) == []


# meiva:note
# The exit status is 0 only when every outcome verified.
def test_the_exit_code_reports_any_failure() -> None:
    assert exit_code([]) == 0
    assert exit_code([_PASS]) == 0
    assert exit_code([_PASS, _FAIL]) == 1
