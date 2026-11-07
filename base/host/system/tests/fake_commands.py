from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Unpack, overload

from base.host.proc import CommandRunner, PopenOptions, run_bounded
from base.host.system.hostport import Host
from base.native_process.os_platform import Platform

# meiva:note
# The two platforms a test picks between. They are values a test chooses, never this process's
# platform: a caller that must name the platform it is observed on reads
# `Platform.current().system`, because a launch receipt is read back against it (#8659).
LINUX_X86 = Platform(
    # meiva:note
    # host-ok: a canned platform a test chooses; the value that must equal the one this process is observed on reads `Platform.current()`, which is what `base/cluster/postgres.py` `read_receipt` compares a launch receipt with (#8659)
    "linux",
    "x86_64",
    "/usr/bin/python3",
)
DARWIN_ARM64 = Platform(
    # meiva:note
    # host-ok: a canned platform a test chooses; the value that must equal the one this process is observed on reads `Platform.current()`, which is what `base/cluster/postgres.py` `read_receipt` compares a launch receipt with (#8659)
    "darwin",
    "arm64",
    "/usr/bin/python3",
)


@dataclass(frozen=True)
class Reply:
    stdout: str | bytes = ""
    stderr: str | bytes = ""
    returncode: int = 0


@dataclass(frozen=True)
class Call:
    argv: tuple[str, ...]
    timeout: float
    kwargs: Mapping[str, Any]


ReplySpec = Reply | BaseException


@dataclass
class _Route:
    matches: Callable[[tuple[str, ...]], bool]
    label: str
    replies: list[ReplySpec]
    repeat: bool


def _shape(value: str | bytes, *, text: bool) -> str | bytes:
    if text:
        return value.decode() if isinstance(value, bytes) else value
    return value if isinstance(value, bytes) else value.encode()


def _starts_with(prefix: tuple[str, ...]) -> Callable[[tuple[str, ...]], bool]:
    def matches(argv: tuple[str, ...]) -> bool:
        return argv[: len(prefix)] == prefix

    return matches


def _is_piped(capture_output: bool, popen_kwargs: Mapping[str, object], stream: str) -> bool:
    return capture_output or popen_kwargs.get(stream) == subprocess.PIPE


def _result(
    command: tuple[str, ...],
    reply: Reply,
    *,
    capture_output: bool,
    popen_kwargs: Mapping[str, object],
    text: bool | None,
) -> subprocess.CompletedProcess[Any]:
    text = bool(text or popen_kwargs.get("encoding"))
    out = err = None
    if _is_piped(capture_output, popen_kwargs, "stdout"):
        out = _shape(reply.stdout, text=text)
    if _is_piped(capture_output, popen_kwargs, "stderr"):
        err = _shape(reply.stderr, text=text)
    return subprocess.CompletedProcess(list(command), reply.returncode, out, err)


class FakeCommands:
    def __init__(self) -> None:
        self.calls: list[Call] = []
        self._routes: list[_Route] = []

    @property
    def argvs(self) -> list[tuple[str, ...]]:
        return [call.argv for call in self.calls]

    def on(
        self,
        match: str | Sequence[str] | Callable[[tuple[str, ...]], bool],
        *replies: ReplySpec,
        repeat: bool = False,
    ) -> None:
        if not replies:
            raise ValueError("a route needs at least one reply")
        if callable(match):
            matches, label = match, "<predicate>"
        else:
            prefix = (match,) if isinstance(match, str) else tuple(match)
            matches, label = _starts_with(prefix), str(list(prefix))
        self._routes.append(_Route(matches, label, list(replies), repeat))

    def _next_reply(self, command: tuple[str, ...]) -> ReplySpec:
        route = next((r for r in self._routes if r.matches(command)), None)
        if route is None:
            raise AssertionError(f"unscripted command: {list(command)}")
        if not route.replies:
            raise AssertionError(f"replies for {route.label} ran out: {list(command)}")
        keep_last = route.repeat and len(route.replies) == 1
        return route.replies[0] if keep_last else route.replies.pop(0)

    def __call__(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        capture_output: bool = False,
        input: bytes | str | None = None,
        text: bool | None = None,
        **popen_kwargs: Unpack[PopenOptions],
    ) -> subprocess.CompletedProcess[Any]:
        command = tuple(argv)
        kwargs: dict[str, Any] = {"capture_output": capture_output, "input": input, **popen_kwargs}
        if text is not None:
            kwargs["text"] = text
        self.calls.append(Call(command, timeout, kwargs))
        reply = self._next_reply(command)
        if isinstance(reply, BaseException):
            raise reply
        return _result(
            command, reply, capture_output=capture_output, popen_kwargs=popen_kwargs, text=text
        )


# meiva:note
# A fake that answers in one shape, as a `CommandRunner`. The port serves callers that read `str` (with
# `text=True`) and callers that read raw bytes, and a test's own fake function returns whichever its
# test wants. This wrapper passes the arguments through untouched and conforms the reply to what the
# caller asked for (`str.encode()` / `bytes.decode()` of the streams), so the fake needs no overloads.
class _AdaptedRunner:
    def __init__(
        self,
        answer: Callable[
            ..., subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]
        ],
    ) -> None:
        self._answer = answer

    @overload
    def __call__(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        capture_output: bool = False,
        input: bytes | str | None = None,
        text: Literal[True],
        **popen_kwargs: Unpack[PopenOptions],
    ) -> subprocess.CompletedProcess[str]: ...

    @overload
    def __call__(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        capture_output: bool = False,
        input: bytes | str | None = None,
        text: Literal[False] | None = None,
        **popen_kwargs: Unpack[PopenOptions],
    ) -> subprocess.CompletedProcess[bytes]: ...

    def __call__(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        capture_output: bool = False,
        input: bytes | str | None = None,
        text: bool | None = None,
        **popen_kwargs: Unpack[PopenOptions],
    ) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
        reply = self._answer(
            argv,
            timeout=timeout,
            capture_output=capture_output,
            input=input,
            text=text,
            **popen_kwargs,
        )
        out: str | bytes | None = reply.stdout
        err: str | bytes | None = reply.stderr
        if text:
            return subprocess.CompletedProcess[str](
                reply.args, reply.returncode, _as_text(out), _as_text(err)
            )
        return subprocess.CompletedProcess[bytes](
            reply.args, reply.returncode, _as_bytes(out), _as_bytes(err)
        )


def _as_text(stream: str | bytes | None) -> str | None:
    return stream.decode() if isinstance(stream, bytes) else stream


def _as_bytes(stream: str | bytes | None) -> bytes | None:
    return stream.encode() if isinstance(stream, str) else stream


type Answer = Callable[..., subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]]


def runner_from(answer: Answer) -> CommandRunner:
    return _AdaptedRunner(answer)


def _as_runner(commands: CommandRunner | Answer | None) -> CommandRunner:
    if commands is None:
        return FakeCommands()
    if isinstance(commands, FakeCommands | _AdaptedRunner):
        return commands
    if commands is run_bounded:
        return run_bounded
    return _AdaptedRunner(commands)


# meiva:note
# `uid` is None by default and read from this process at the call: the account a test's
# files belong to is a fact about the machine running the test, so a canned id would make
# the host agree with them only where that id happens to be the runner's (#7529, #7648).
# A test that means a specific account passes it.
def fake_host(
    commands: CommandRunner | Answer | None = None,
    *,
    platform: Platform = LINUX_X86,
    programs: Mapping[str, str] | None = None,
    uid: int | None = None,
    environ: Mapping[str, str] | None = None,
) -> Host:
    found = dict(programs or {})
    return Host(
        run=_as_runner(commands),
        which=found.get,
        platform=platform,
        uid=os.geteuid() if uid is None else uid,
        environ={} if environ is None else environ,
    )


# meiva:note
# A test's host whose `run` is the real runner: a test that starts this machine's own programs
# (`postgres`, `redis-server`, `git`) needs them to run, and says so once here instead of reading
# the process's platform. Everything else stays a test value: `which` answers None, so a component
# that wants a program through the host fails loudly rather than resolving this process's PATH.
def real_commands_host(*, environ: Mapping[str, str] | None = None) -> Host:
    return fake_host(commands=run_bounded, environ=environ)
