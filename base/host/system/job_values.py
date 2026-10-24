import shlex
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

__all__ = [
    "LAUNCHD_LABEL_PREFIX",
    "JobPaths",
    "JobRoots",
    "cron_environment",
    "launchd_environment",
    "plist_path",
]

LAUNCHD_LABEL_PREFIX = "com.meiva"


# meiva:note
# Converge supplies these already captured command values before any job-specific I/O.
@dataclass(frozen=True)
class JobRoots:
    home: Path
    repo: Path
    user_home: Path


@dataclass(frozen=True)
class JobPaths:
    roots: JobRoots
    meiva: str


def plist_path(label: str, *, user_home: Path) -> Path:
    return user_home / "Library" / "LaunchAgents" / f"{label}.plist"


def cron_environment(home: Path) -> str:
    return f"MEIVA_HOME={shlex.quote(str(home))} "


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
