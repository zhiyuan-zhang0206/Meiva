from __future__ import annotations

import shlex
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

__all__ = [
    "DIRECTORY_PARTS",
    "ERROR_LOG_NAME",
    "EXPIRING_WITHIN_DAYS",
    "HEARTBEAT_ENV",
    "HEARTBEAT_WRITE_FAILED_RC",
    "LAUNCHD_LABEL_PREFIX",
    "SCHEMA",
    "DailyAt",
    "DuplicateHeartbeatName",
    "EverySeconds",
    "HeartbeatDeclaration",
    "HeartbeatDeclarationError",
    "HeartbeatRender",
    "InvalidHeartbeatDeclaration",
    "JobHeartbeat",
    "JobPaths",
    "JobRoots",
    "JobSchedule",
    "MissingHeartbeatDeclaration",
    "NoHeartbeat",
    "RenderContext",
    "cron_environment",
    "heartbeat_path",
    "launchd_domain",
    "launchd_environment",
    "one_line_without_comment_end",
    "plist_path",
]

LAUNCHD_LABEL_PREFIX = "com.meiva"


# meiva:note
# Converge supplies these already captured command values before any job-specific I/O.
#
# The three fields are all paths, so a swapped argument is invisible to the type checker: they
# are keyword-only, and each must be absolute (a relative path would follow the working
# directory of whoever registers the job).
@dataclass(frozen=True, kw_only=True)
class JobRoots:
    home: Path
    repo: Path
    user_home: Path

    def __post_init__(self) -> None:
        for name, path in (("home", self.home), ("repo", self.repo), ("user_home", self.user_home)):
            if not path.is_absolute():
                raise ValueError(f"JobRoots.{name} must be an absolute path, got {path!r}")


@dataclass(frozen=True)
class JobPaths:
    roots: JobRoots
    meiva: str


def plist_path(label: str, *, user_home: Path) -> Path:
    return user_home / "Library" / "LaunchAgents" / f"{label}.plist"


# meiva:note
# The variable assignments a crontab line carries ahead of its command; `extra` is the job's own
# declarations (the heartbeat file path), quoted the same way as the home.
def cron_environment(home: Path, extra: tuple[tuple[str, str], ...] = ()) -> str:
    entries = (("MEIVA_HOME", str(home)), *extra)
    return " ".join(f"{key}={shlex.quote(value)}" for key, value in entries) + " "


def launchd_environment(
    *, home: Path, path_env: str, indent: str, extra: tuple[tuple[str, str], ...]
) -> str:
    entries = {"MEIVA_HOME": str(home), "PATH": path_env, **dict(extra)}
    return (
        f"{indent}<key>EnvironmentVariables</key>\n"
        f"{indent}<dict>\n"
        + "".join(
            f"{indent}    <key>{escape(key)}</key>\n{indent}    <string>{escape(value)}</string>\n"
            for key, value in entries.items()
        )
        + f"{indent}</dict>"
    )


# meiva:note
# The values one generated OS job definition carries: its label, the environment the scheduler
# hands it, where its plist or crontab entry lives, and — since a definition that writes nothing
# cannot be told from one that was removed — the heartbeat it declares.
#
# The declaration is built beside the trigger it renders (`*_content.py`), so the plist and the
# expectation cannot describe different jobs; `cron/heartbeat.py` turns it into the artifact and
# refuses a job that declares none. The refusals are a value check, so they live with the values.

SCHEMA = 1

# meiva:note
# The one variable a rendered job definition carries: the absolute path of the heartbeat file the
# run must write. One path rather than a directory and a name, so the command side has nothing to
# join and no way to pair the two wrongly.
HEARTBEAT_ENV = "MEIVA_JOB_HEARTBEAT"

# meiva:note
# The exit code a job reports when it could not write its own heartbeat and its own code was 0.
# It must differ from the command's own failure code: "ran and failed" and "ran, failed, and could
# not say so" have different fixes, and a shared code would merge them in whatever reads the
# scheduler. A run that did fail keeps its own code: that code is the primary fact an owner acts
# on, and the missing record is already said on stderr and in `ERROR_LOG_NAME`.
HEARTBEAT_WRITE_FAILED_RC = 3

DIRECTORY_PARTS = ("state", "heartbeats")

# meiva:note
# The degraded channel beside `DIRECTORY_PARTS` (never inside it, so a reader that globs `*.json`
# there cannot pick up an error line as a record): one place a run appends to when it could not
# write its heartbeat, so the reason outlives the stderr of a job nobody was watching.
ERROR_LOG_NAME = "heartbeat-errors.log"

# meiva:note
# The window in which an `until` exception is reported as expiring rather than live. Seven days
# is the longest a person can be expected to pick the work up inside one working week; a shorter
# window would surface on the day it must be renewed.
EXPIRING_WITHIN_DAYS = 7

EXCEPTION_DECLARED = "none"


# meiva:note
# A job's heartbeat declaration cannot be rendered as it stands.
class HeartbeatDeclarationError(ValueError):
    pass


# meiva:note
# A job renders a definition but declares no heartbeat for it.
class MissingHeartbeatDeclaration(HeartbeatDeclarationError):  # noqa: N818
    pass


# meiva:note
# A heartbeat declaration is malformed: a missing field, or a value the artifact cannot carry.
class InvalidHeartbeatDeclaration(HeartbeatDeclarationError):  # noqa: N818
    pass


# meiva:note
# Two jobs on one machine declare the same heartbeat name, so one file would answer for both.
class DuplicateHeartbeatName(HeartbeatDeclarationError):  # noqa: N818
    pass


# meiva:note
# The cadence a job runs at, declared as the values the trigger is rendered from, not as text.
# `*_content.py` builds one and renders both the plist trigger and the declaration from it, so a
# heartbeat cannot claim a schedule the scheduler was never told.
@dataclass(frozen=True, kw_only=True)
class DailyAt:
    hour: int
    minute: int

    def __post_init__(self) -> None:
        if not 0 <= self.hour <= 23:
            raise ValueError(f"DailyAt.hour must be an hour of the day, got {self.hour!r}")
        if not 0 <= self.minute <= 59:
            raise ValueError(f"DailyAt.minute must be a minute of the hour, got {self.minute!r}")

    def described(self) -> str:
        return f"daily-at-{self.hour:02d}:{self.minute:02d}"

    def interval_seconds(self) -> int:
        return 86_400


@dataclass(frozen=True, kw_only=True)
class EverySeconds:
    seconds: int

    def __post_init__(self) -> None:
        if self.seconds <= 0:
            raise ValueError(f"EverySeconds.seconds must be positive, got {self.seconds!r}")

    def described(self) -> str:
        return f"every-{self.seconds}s"

    def interval_seconds(self) -> int:
        return self.seconds


JobSchedule = DailyAt | EverySeconds


def one_line_without_comment_end(value: str, *, field: str) -> str:
    text = value.strip()
    if not text:
        raise InvalidHeartbeatDeclaration(f"a heartbeat {field} must not be empty")
    if "\n" in text or "--" in text:
        raise InvalidHeartbeatDeclaration(
            f"a heartbeat {field} is rendered into an XML comment and must be one line "
            f"without '--', got {value!r}"
        )
    return text


# meiva:note
# The declaration a job that does carry a heartbeat makes: its file, who to call when the file
# stops moving, and how late is too late.
#
# The tolerance is `late_after_seconds` rather than a "grace": nothing in this repository waits on
# it, it is handed to a reader elsewhere that compares it with its own clock, and a name the clock
# lattice owns would read as a timer this process starts.
@dataclass(frozen=True, kw_only=True)
class JobHeartbeat:
    name: str
    owner: str
    late_after_seconds: int

    def __post_init__(self) -> None:
        name = self.name.strip()
        if not name or "/" in name or name in (".", ".."):
            raise InvalidHeartbeatDeclaration(
                f"a heartbeat name is one path segment (it becomes <name>.json), got {self.name!r}"
            )
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "owner", one_line_without_comment_end(self.owner, field="owner"))
        if self.late_after_seconds <= 0:
            raise InvalidHeartbeatDeclaration(
                f"a heartbeat tolerance must be positive seconds, got {self.late_after_seconds!r}"
            )


# meiva:note
# The declared exception: this job is watched somewhere else, or by no one, until a stated day.
# `reason` is why, `owner` is who answers for it, `until` is when the exception stops being one.
# An expired declaration still renders — refusing would fail a converge on the day of expiry,
# and the cost of that would land on whoever happens to be deploying, who is usually not the
# owner — but it flips to `expired` in the artifact and to a critical reading downstream.
@dataclass(frozen=True, kw_only=True)
class NoHeartbeat:
    reason: str = ""
    owner: str = ""
    until: date | None = None


HeartbeatDeclaration = JobHeartbeat | NoHeartbeat


# meiva:note
# The machine a definition is being rendered for: whose home the file lands in, which machine the
# expectation belongs to, which launchd domain holds the job, and the day an exception's window is
# counted from. Values, so a render is reproducible without a clock or a filesystem.
@dataclass(frozen=True, kw_only=True)
class RenderContext:
    home: Path
    machine: str
    uid: int
    today: date

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "machine", one_line_without_comment_end(self.machine, field="machine")
        )


# meiva:note
# The validated product a renderer splices into the artifact: the environment entries the job
# definition carries, and the one-line statement of what this machine now expects.
@dataclass(frozen=True, kw_only=True)
class HeartbeatRender:
    job: str
    file: Path | None
    environment: tuple[tuple[str, str], ...]
    artifact: str


# meiva:note
# The file a job writes when it finishes: inside the home, never an agent workspace — a
# workspace outlives the agent that owns it by minutes at most.
def heartbeat_path(home: Path, name: str) -> Path:
    return home.joinpath(*DIRECTORY_PARTS) / f"{name}.json"


# meiva:note
# The launchd domain these jobs are bootstrapped into: `gui/<uid>`, the same domain
# `mechanics.reload_launchd_job` bootstraps into, and what a reader queries `print-disabled` with.
def launchd_domain(uid: int) -> str:
    if uid <= 0:
        raise ValueError(f"a launchd domain needs a real user id, got {uid!r}")
    return f"gui/{uid}"
