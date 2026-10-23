# meiva:note
# The text half of the LLM repair step as values: the repair prompt, the feedback sent back when an
# attempt still fails to compile, a SyntaxError rendered the way Python's own traceback shows it, and
# the cleanup of the model's reply.
from __future__ import annotations

import re
from typing import Any, cast

REPAIR_MAX_ATTEMPTS = 3

REPAIR_SYSTEM = (
    "You are a Python syntax repair tool. The user message contains Python "
    "source that failed to compile, followed by its SyntaxError. Return the "
    "corrected source code and nothing else -- no markdown fences, no prose, no "
    "commentary. Make the smallest change that resolves the error while "
    "preserving the code's intent and structure; do not add or remove unrelated "
    "lines. Frequent causes: a multi-line value written with single/double "
    "quotes that should be a triple-quoted string; a triple-quoted string whose "
    "body contains an unescaped triple-quote or ends with a quote adjacent to "
    "the closing delimiter; an f-string with an empty or malformed `{}` field."
)

_FENCE_RE = re.compile(r"^\s*```[^\n]*\n(.*?)\n```\s*$", re.DOTALL)


def repair_request(code: str, rendered_error: str) -> str:
    return f"{code}\n\n# --- compile error ---\n{rendered_error}"


def retry_request(rendered_error: str) -> str:
    return (
        "That still fails to compile:\n"
        f"{rendered_error}\n"
        "Return the full corrected source again, code only."
    )


# meiva:note
# A SyntaxError as Python's native traceback would show it: a `File ... line N` header, the offending
# source line, a caret and the `SyntaxError: msg` line. Used for the repair prompt and for the
# fallback message the agent receives. compile() does not set `.text` when the source is a string,
# so the offending line is sliced out of the source when it is missing.
def render_syntax_error(e: SyntaxError, source: str) -> str:
    lines: list[str] = []
    if e.lineno:
        lines.append(f'  File "<agent_code>", line {e.lineno}')
        source_line = e.text.rstrip() if e.text else ""
        if not source_line:
            code_lines = source.split("\n")
            if e.lineno <= len(code_lines):
                source_line = code_lines[e.lineno - 1]
        if source_line:
            lines.append(f"    {source_line}")
        if e.offset:
            lines.append(f"    {' ' * max(0, e.offset - 1)}^")
    lines.append(f"SyntaxError: {e.msg}")
    return "\n".join(lines)


# meiva:note
# A model reply flattened to plain text. A thinking model returns a list of blocks (thinking and
# text); only the text blocks are kept.
def extract_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in cast(list[object], content):
            if isinstance(block, dict):
                fields = cast(dict[str, Any], block)
                if fields.get("type") == "text" and isinstance(fields.get("text"), str):
                    parts.append(fields["text"])
        return "".join(parts)
    return ""


# meiva:note
# One surrounding markdown code fence stripped, for a model that wrapped its output despite
# instructions not to.
def strip_code_fence(text: str) -> str:
    match = _FENCE_RE.match(text)
    return match.group(1) if match else text.strip()
