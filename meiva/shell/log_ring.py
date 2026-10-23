# meiva:note
# The per-workspace ring of background-run logs: where they live, and which ones to evict, as functions of values. `background.py` reads the directory and removes what this names.
# meiva:link meiva/shell/docs/shell.meiva.okf.md
from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path

# meiva:note
# Background command output is teed to `.shell_logs/` inside the agent's workspace and the session capture. The log survives session close and scrollback limits and lives at a predictable spot: the workspace is the default base of the agent's file and shell tools, so `.shell_logs/<sid>_<name>.log` is readable by relative path, and the path is reconstructable from the session id alone. Sibling of `.exec_output/` (oversized exec-output overflow), with its own ring so the two cannot evict each other.
OUTPUT_DIRNAME = ".shell_logs"

# meiva:note
# Ring: keep the most recent N logs per workspace and prune older ones, so the directory does not grow unbounded across a long-lived agent.
OUTPUT_KEEP = 20


def output_dir(workspace: Path) -> Path:
    return workspace / OUTPUT_DIRNAME


def log_path(directory: Path, session_id: int, name: str) -> Path:
    return directory / f"{session_id}_{name}.log"


# meiva:note
# One log file of the ring: its name and when it was last written.
@dataclass(frozen=True)
class LogFile:
    name: str
    modified: float


# meiva:note
# The names to evict once the ring is over its size, oldest first. A log whose session is still alive is never evicted, even past the ring size: a long-lived watcher's log keeps its spawn-time mtime for its whole lifetime (a time watcher sleeps between fires and only writes its schedule-state announcement lines, template v3, see base/daemon/schedules/watcher.py), so mtime-LRU alone would evict it while its process still holds the fd, orphaning the output and breaking the exit notice's tail. Live sessions outrank the ring cap; the cap re-applies once they end.
def evictions(logs: Sequence[LogFile], live: Collection[int], keep: int = OUTPUT_KEEP) -> list[str]:
    ordered = sorted(logs, key=lambda log: log.modified)
    victims: list[str] = []
    for old in ordered[: max(len(ordered) - keep, 0)]:
        session_id = old.name.split("_", 1)[0]
        if session_id.isdigit() and int(session_id) in live:
            continue
        victims.append(old.name)
    return victims
