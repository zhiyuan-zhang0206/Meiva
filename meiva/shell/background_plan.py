# meiva:note
# The single line a background session executes (the command, its log tee, the exit marker and the completion notice) and the launch file that carries it, as functions of values. `background.py` reads the process and the disk and runs what these describe.
# meiva:link meiva/shell/docs/shell.meiva.okf.md
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import assert_never

# meiva:note
# How long a closing run (keep=False) waits for its log to catch up, and how often it looks. The shell exits the moment the command does, and the session close then kills the process group; a `tee` still draining the pipe dies with the command's last lines in it, leaving a log that exists (`allocate_output_path` touches it) and stays empty. The wait is bounded so a grandchild that inherited stdout cannot postpone the notice, and it costs one `tail` in the normal case.
LOG_FLUSH_WAIT_S = 1.0
LOG_FLUSH_POLL_S = 0.05

# meiva:note
# Bytes read from the end of the log when looking for the run's exit marker, enough to cover the marker plus a command line written without a trailing newline.
EXIT_TAIL_BYTES = 128

# meiva:note
# Characters that stay shell-active inside a double-quoted string.
_DQUOTE_UNSAFE = re.compile(r'[$`"\\]')


# meiva:note
# The per-run completion-notice policies a caller may name. A run that names none delegates to the agent's own policy.
class NotifyPolicy(StrEnum):
    ALWAYS = "always"


# meiva:note
# An explicit per-run policy must be one of the known ones; None delegates to the agent's policy.
def notify_policy(notify: str | None) -> NotifyPolicy | None:
    if notify is None:
        return None
    try:
        return NotifyPolicy(notify)
    except ValueError:
        known = [policy.value for policy in NotifyPolicy]
        raise ValueError(f"notify must be one of {known}, got {notify!r}") from None


# meiva:note
# The launch file removes itself, then runs the line; the session sources it, so a long line stays out of a canonical-mode PTY input buffer.
def launch_file_text(line: str, path: Path) -> str:
    return f"command rm -f -- {shlex.quote(str(path))}\n{line}\n"


def source_command(path: Path) -> str:
    return f". {shlex.quote(str(path))}"


# meiva:note
# Who hears about the run and how: the agent, the label and source the notice names, and the CLI that delivers it.
@dataclass(frozen=True)
class Notice:
    agent_id: int
    label: str
    source: str
    cli: Path


# meiva:note
# The line a background session runs. The notice rides inside a double-quoted shell string where only ${_ec} may expand: callers build label and path from validated slugs and framework paths, and this re-declares that invariant so a future caller cannot silently reopen expansion via $ ` " or backslash. The join is on the log, not on the writer: `exec 3> >(tee ...)` is not a waitable child on bash 3.2 (what macOS ships), so a closing run waits for its own exit marker to appear at the end of the log instead. That marker is the last thing the writer appends, which makes "the log is complete" the exact condition the shell waits for, on any bash.
def notified_line(
    cmd: str,
    notice: Notice,
    *,
    output_path: Path,
    keep: bool,
    notify: NotifyPolicy | None = None,
) -> str:
    if "\n" in cmd or "\r" in cmd:
        raise ValueError(
            "cmd must be a single line — join steps with ';' or '&&', or write a "
            "script file and run that"
        )
    for part, what in ((notice.label, "label"), (str(output_path), "output_path")):
        if _DQUOTE_UNSAFE.search(part):
            raise ValueError(f"{what} contains shell-active characters: {part!r}")
    q_path = shlex.quote(str(output_path))
    text = f"{notice.label} finished. Full output at {output_path}."
    send_notice = (
        f'{shlex.quote(str(notice.cli))} agents send {notice.agent_id} "{text}" '
        f"--source {notice.source} --tail-file {q_path}"
    )
    line = (
        f"exec 3> >(tee {q_path}); ( {cmd} ) >&3 2>&1; _ec=$?; "
        f"printf '[exit %s]\\n' \"$_ec\" >&3; exec 3>&-; "
    )
    if not keep:
        polls = int(LOG_FLUSH_WAIT_S / LOG_FLUSH_POLL_S)
        line += (
            f'_polls=0; while [ "$_polls" -lt {polls} ]; do '
            f'case "$(tail -c {EXIT_TAIL_BYTES} {q_path} 2>/dev/null)" in '
            f'*"[exit $_ec]") break;; esac; '
            f"sleep {LOG_FLUSH_POLL_S}; _polls=$((_polls + 1)); done; "
        )
    match notify:
        case NotifyPolicy.ALWAYS:
            line += send_notice
        case None:
            line += f"{send_notice} --completion"
        case _:
            assert_never(notify)
    if not keep:
        line += "; exit $_ec"
    return line
