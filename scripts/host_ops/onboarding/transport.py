from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass

LOCAL_HOST_NAMES = frozenset({"local", "localhost"})

# meiva:note
# Every command runs under this one search path. An ssh or agent session starts
# without the interactive profile, so an installed CLI is indistinguishable from
# a missing one; these are the directories the harness probe falls back to, plus
# the system paths a trimmed PATH loses.
# meiva:link scripts/host_ops/docs/harness-onboarding.meiva.okf.md
PATH_PREAMBLE = (
    'export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$HOME/.bun/bin:'
    '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"'
)

SSH_OPTIONS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=10")


@dataclass(frozen=True)
class Result:
    host: str
    command: str
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def json_payload(self) -> dict[str, object]:
        for raw_line in reversed(self.stdout.strip().splitlines()):
            line = raw_line.strip()
            if line.startswith("{"):
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    return parsed
        raise ValueError(
            f"{self.host}: no JSON on stdout of {self.command!r}: {self.stderr[-400:]}"
        )


class Host:
    def __init__(self, name: str) -> None:
        if not name:
            raise ValueError("a host needs a name ('local' for this machine)")
        self.name = name

    @property
    def is_local(self) -> bool:
        return self.name in LOCAL_HOST_NAMES

    def __str__(self) -> str:
        return self.name

    def run(self, command: str, *, stdin: bytes | None = None, timeout: float = 300.0) -> Result:
        script = f"{PATH_PREAMBLE}\n{command}\n"
        if self.is_local:
            argv = ["bash", "-c", script]
            payload = stdin
        elif stdin is None:
            argv = ["ssh", *SSH_OPTIONS, self.name, "bash", "-s"]
            payload = script.encode("utf-8")
        else:
            argv = ["ssh", *SSH_OPTIONS, self.name, "bash", "-c", shlex.quote(script)]
            payload = stdin
        return self._execute(argv, command, stdin=payload, timeout=timeout)

    def python(
        self, source: str, request: dict[str, object] | None = None, *, timeout: float = 300.0
    ) -> Result:
        payload = bind_request(source, request or {})
        return self.run("python3 -", stdin=payload.encode("utf-8"), timeout=timeout)

    def _execute(
        self, argv: list[str], command: str, *, stdin: bytes | None, timeout: float
    ) -> Result:
        try:
            # meiva:note
            # argv is assembled from a validated host name and fixed options; no
            # caller-supplied string reaches it unquoted, because a run() payload
            # travels on stdin and every other argument is a literal or a path.
            # meiva:link scripts/host_ops/docs/harness-onboarding.meiva.okf.md
            completed = subprocess.run(  # noqa: S603
                argv, input=stdin, capture_output=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired:
            return Result(self.name, command, 124, "", f"timed out after {timeout:.0f}s")
        return Result(
            self.name,
            command,
            completed.returncode,
            completed.stdout.decode("utf-8", "replace"),
            completed.stderr.decode("utf-8", "replace"),
        )


def bind_request(source: str, request: dict[str, object]) -> str:
    assignment = "__REQUEST__ = " + repr(request) + "\n"
    lines = source.splitlines(keepends=True)
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].startswith("if __name__"):
            return "".join(lines[:index]) + assignment + "".join(lines[index:])
    return source + "\n" + assignment


def parse_hosts(values: list[str] | None) -> list[Host]:
    hosts: list[Host] = []
    for name in values or ["local"]:
        host = Host(name)
        if all(existing.name != host.name for existing in hosts):
            hosts.append(host)
    return hosts
