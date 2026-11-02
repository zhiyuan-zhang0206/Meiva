# meiva:note
# The single answer to "are these bytes a source file's text?", shared by the lint toolbox and the
# lints' own rules. It is a decision, not an effect: it reads nothing, so a caller that already
# holds the bytes (a walk that read them, a diff that was given them) can ask without paying for a
# second read. `lint_common.read_utf8_text` is the reader, and it decides with this.
from __future__ import annotations

__all__ = ["decode_source"]

# meiva:note
# A NUL byte in the first 8 KiB marks a binary file — a NUL later in a genuine text file (one
# inside a string literal) does not. Undecodable bytes are not source either.
_BINARY_WINDOW = 8192


# meiva:note
# The text these bytes are, or None when they are not source text.
def decode_source(data: bytes) -> str | None:
    if b"\x00" in data[:_BINARY_WINDOW]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None
