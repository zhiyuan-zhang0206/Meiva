import re

from scripts.content_lint.code_comments.model import NoteKind

_LINK = re.compile(r"(?:#|//)\s*meiva:link\s+(\S+)")
_NOTE = re.compile(r"(?:#|//)\s*meiva:note\s*")
_TIME_BOMB = re.compile(r"# time-bomb-ok: \S.*")
_RUFF_CODE = r"[A-Z]+[0-9]{3,4}"
_PYRIGHT_CODE = r"report[A-Z][A-Za-z0-9]+"
_LINT = re.compile(
    rf"# noqa: {_RUFF_CODE}(?:, ?{_RUFF_CODE})*"
    rf"|# pyright: ignore\[{_PYRIGHT_CODE}(?:, ?{_PYRIGHT_CODE})*\]"
)


def note_header(text: str) -> bool:
    return _NOTE.fullmatch(text) is not None


def link_target(text: str) -> str | None:
    match = _LINK.fullmatch(text)
    return match[1] if match is not None else None


def comment_text(text: str) -> str:
    body = text[1:]
    return body[1:] if body.startswith(" ") else body


def comment_kind(text: str) -> NoteKind:
    if _TIME_BOMB.fullmatch(text):
        return NoteKind.MACHINE
    if _LINT.fullmatch(text):
        return NoteKind.LINT_DIRECTIVE
    return NoteKind.COMMENT
