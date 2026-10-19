from __future__ import annotations

import sys
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, TextIO

# meiva:note
# A harness child reaches the cluster through an attachment, and the bridge wants one
# command it can run without writing a bootstrap file. `exec` therefore reads the
# script the same way `impersonate exec` does (a file, or stdin as `-`) and runs it
# inside the attachment, so the child's own `meiva.*` calls carry the borrowed
# identity. The opener is injected so the command is testable without a cluster.
# meiva:link meiva/self/harness/docs/harness-attach.meiva.okf.md
AttachmentOpener = Callable[..., AbstractContextManager[Any]]


def cmd_harness_exec(
    *,
    file: str | None,
    handle: str | None,
    agent_id: int | None,
    session_id: str | None,
    opener: AttachmentOpener | None = None,
    stdin: TextIO | None = None,
) -> int:
    try:
        return _exec(
            file=file,
            handle=handle,
            agent_id=agent_id,
            session_id=session_id,
            opener=opener,
            stdin=sys.stdin if stdin is None else stdin,
        )
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"harness: {exc}", file=sys.stderr)
        return 1


def _exec(
    *,
    file: str | None,
    handle: str | None,
    agent_id: int | None,
    session_id: str | None,
    opener: AttachmentOpener | None,
    stdin: TextIO,
) -> int:
    import meiva

    code = stdin.read() if file is None or file == "-" else Path(file).read_text(encoding="utf-8")
    if not code.strip():
        raise ValueError("Python input must be nonempty")
    open_attachment = opener if opener is not None else _default_opener()
    with open_attachment(handle, agent_id=agent_id, session_id=session_id):
        exec(
            compile(code, file or "<meiva-harness>", "exec"),
            {"__name__": "__main__", "meiva": meiva},
        )
    return 0


def _default_opener() -> AttachmentOpener:
    from meiva.self.harness import open_attachment

    return open_attachment
