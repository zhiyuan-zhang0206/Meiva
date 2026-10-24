# meiva:note
# What a coding-tool launch asks for, checked, and the messages a launch composes from it, as functions of values. The launchers (`claude`, `codex`, `antigravity`) read the process and run what these describe.
# meiva:link meiva/shell/coding_tools/docs/coding-tools.meiva.okf.md
from __future__ import annotations

import argparse
import uuid
from dataclasses import dataclass
from pathlib import Path


# meiva:note
# One launch's inputs: a supervised worker carries both files; a prompt launch carries an initial prompt and no files. `resume` is the tool session id to reopen, `provenance` the rendered dispatch block that leads the first message.
@dataclass(frozen=True)
class LaunchRequest:
    workspace: Path
    tasks_file: Path | None
    work_file: Path | None
    ttl_seconds: float
    caller_instance: str | None
    prompt: str | None
    skill_dir: Path
    resume: str | None
    provenance: str | None = None


# meiva:note
# The initial prompt, or None for a supervised launch. A prompt launch needs a non-empty prompt and no files; a supervised launch needs both files.
def checked_prompt(
    prompt: str | None, tasks_file: Path | None, work_file: Path | None
) -> str | None:
    if prompt is not None:
        if not prompt.strip():
            raise ValueError("a prompt launch needs a non-empty prompt")
        if tasks_file is not None or work_file is not None:
            raise ValueError("a prompt launch does not use task or work files")
        return prompt
    if tasks_file is None or work_file is None:
        raise ValueError("a supervised launch needs its task and work files")
    return None


def contract_file(skill_dir: Path) -> Path:
    return skill_dir / "references" / "collaboration_protocol.md"


# meiva:note
# Parse a `--resume` argument: the tool session id a launch printed (a UUID).
def session_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a session id; pass the UUID the launch printed"
        ) from exc


# meiva:note
# An already-rendered dispatch block leads the message, so the worker reads who dispatched it before the contract and the two file paths. A takeover never reaches this function: its briefing carries the three-line form and creates no files. A resumed session is told it was interrupted and continues from its own history instead of starting over.
def worker_bootstrap(
    contract: Path,
    workspace: Path,
    tasks_file: Path,
    work_file: Path,
    *,
    resumed: bool = False,
    provenance: str | None = None,
) -> str:
    interrupted = (
        "This session was interrupted (its shell was closed) and has been resumed with "
        "its history. "
        if resumed
        else ""
    )
    return (f"{provenance}\n\n" if provenance else "") + (
        f"{interrupted}Read the collaboration contract at {contract} and follow it. "
        f"Your workspace is {workspace}. "
        f"Your task file (read-only for you) is {tasks_file}. "
        f"Your work file (yours to write, STATUS + log) is {work_file}. "
        + (
            "Re-read the task file and your work file, then continue from where you stopped."
            if resumed
            else "Now read the task file and start working."
        )
    )
