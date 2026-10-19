# meiva:note
# Direct sentinel for a stalled database write path. PgBouncer's per-minute
# stats line is the earliest host-local evidence that transactions stopped
# completing; over 29 days of history the rule below (three consecutive minutes
# under 20 xacts/s with an average query time above 300 ms) marked only the two
# real incidents and no quiet night. It is the root-cause signal that the
# agent-host crash wave is the consequence of.
# meiva:link schedules/README.md
from __future__ import annotations

import json
import re
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

import meiva

REPORT_AGENTS = (1818, 405)
POLL_SECONDS = 60
WINDOW = 3
MIN_XACTS = 20
MAX_QUERY_US = 300_000
COOLDOWN_SECONDS = 1800
STATS = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+) CST \[\d+\] LOG stats: "
    r"(\d+) xacts/s, (\d+) queries/s,.*?, xact \d+ us, query (\d+) us, wait (\d+) us"
)


def log_path() -> Path:
    return Path.home() / ".meiva" / "pgbouncer" / "pgbouncer.log"


def state_path() -> Path:
    return Path.home() / ".meiva" / "run" / "db-write-stall-watch.json"


def tail_samples(path: Path, *, count: int = 40) -> list[tuple[str, int, int, int]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-count:]
    except OSError:
        return []
    samples: list[tuple[str, int, int, int]] = []
    for line in lines:
        match = STATS.match(line)
        if match is not None:
            samples.append((match[1], int(match[2]), int(match[3]), int(match[4])))
    return samples


def read_last_alert(path: Path) -> float:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0.0
    value = payload.get("last_alert")
    return float(value) if isinstance(value, (int, float)) else 0.0


def write_state(path: Path, *, last_alert: float, last_sample: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "last_alert": last_alert,
                "last_sample": last_sample,
                "updated": datetime.now(UTC).isoformat(timespec="seconds"),
            }
        ),
        encoding="utf-8",
    )


def is_stalled(sample: tuple[str, int, int, int]) -> bool:
    return sample[1] < MIN_XACTS and sample[3] > MAX_QUERY_US


def confirmed(samples: list[tuple[str, int, int, int]]) -> bool:
    return len(samples) >= WINDOW and all(is_stalled(item) for item in samples[-WINDOW:])


def describe(samples: list[tuple[str, int, int, int]]) -> str:
    return "; ".join(
        f"{ts} {xacts} xacts/s query={query_us / 1000:.0f}ms" for ts, xacts, _, query_us in samples
    )


def report(text: str) -> None:
    for agent in REPORT_AGENTS:
        try:
            meiva.agents.send_message(agent, text)
        except Exception as exc:
            print(f"db-write-stall-watch could not notify {agent}: {exc}", flush=True)


def main() -> None:
    seen: str | None = None
    samples: deque[tuple[str, int, int, int]] = deque(maxlen=12)
    log = log_path()
    state = state_path()
    last_alert = read_last_alert(state)
    alerted = False
    while True:
        try:
            for sample in tail_samples(log):
                if seen is not None and sample[0] <= seen:
                    continue
                seen = sample[0]
                samples.append(sample)
                write_state(state, last_alert=last_alert, last_sample=seen)
                if is_stalled(sample):
                    print(
                        f"[{datetime.now(UTC):%H:%M:%SZ}] stalled: {describe([sample])}", flush=True
                    )
                if confirmed(list(samples)):
                    if time.time() - last_alert >= COOLDOWN_SECONDS:
                        last_alert = time.time()
                        write_state(state, last_alert=last_alert, last_sample=seen)
                        alerted = True
                        report(
                            "DB write path stalled for >= %d consecutive minutes "
                            "(xacts/s < %d and average query > %d ms):\n%s\n"
                            "This is the root-cause signal; agent-host crashes are its consequence. "
                            "Check host memory and the largest cgroup units "
                            "(~/.meiva/logs/host-memory.jsonl)."
                            % (WINDOW, MIN_XACTS, MAX_QUERY_US // 1000, describe(list(samples)))
                        )
                elif alerted:
                    alerted = False
                    print(
                        f"[{datetime.now(UTC):%H:%M:%SZ}] recovered: {describe([sample])}",
                        flush=True,
                    )
        except Exception as exc:
            print(
                f"[{datetime.now(UTC):%H:%M:%SZ}] probe failed: {type(exc).__name__}: {exc}",
                flush=True,
            )
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
