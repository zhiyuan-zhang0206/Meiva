# meiva:link base/lm/harness/docs/cli-delivery.md

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

REMOTES_ENV = "MEIVA_ZERO_TRACE_GIT_REMOTES"
AGENT_ID_ENV = "MEIVA_AGENT_ID"
HANDLE_ENV = "MEIVA_HARNESS_HANDLE"
HOME_ENV = "MEIVA_HOME"

BLOCKED_EXIT_CODE = 1
PLATFORM_WORDS = ("meiva", "meizel", "antigravity")
COMMIT_SCAN_LIMIT = 500

_GLOBAL_VALUE_OPTIONS = frozenset(
    {
        "-C",
        "-c",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--super-prefix",
        "--config-env",
        "--attr-source",
    }
)
_GLOBAL_REPLAY_DROPPED = frozenset({"-p", "--paginate", "-P", "--no-pager"})

_PUSH_LONG_OPTIONS: Mapping[str, bool] = {
    "all": False,
    "branches": False,
    "mirror": False,
    "tags": False,
    "follow-tags": False,
    "delete": False,
    "prune": False,
    "dry-run": False,
    "porcelain": False,
    "force": False,
    "force-with-lease": False,
    "force-if-includes": False,
    "atomic": False,
    "set-upstream": False,
    "thin": False,
    "verbose": False,
    "quiet": False,
    "progress": False,
    "verify": False,
    "signed": False,
    "ipv4": False,
    "ipv6": False,
    "repo": True,
    "receive-pack": True,
    "exec": True,
    "push-option": True,
    "recurse-submodules": True,
}
_PUSH_SHORT_VALUE_OPTIONS = frozenset("o")

_TIP_FLAGS = frozenset({"--branches", "--tags", "--all"})

_TRAILER_PATTERNS = (
    ("an AI attribution trailer", re.compile(r"co-authored-by:.*(meiva|claude)", re.IGNORECASE)),
    ("an AI generation footer", re.compile(r"generated with.*claude code", re.IGNORECASE)),
    ("a platform path", re.compile(r"\.meiva/", re.IGNORECASE)),
)


@dataclass(frozen=True)
class GitInvocation:
    replay: tuple[str, ...]
    subcommand: str | None
    args: tuple[str, ...]


def parse_invocation(argv: Sequence[str]) -> GitInvocation:
    replay: list[str] = []
    index = 0
    while index < len(argv):
        arg_text = argv[index]
        if not arg_text.startswith("-") or arg_text == "-":
            return GitInvocation(tuple(replay), arg_text, tuple(argv[index + 1 :]))
        name = arg_text.split("=", 1)[0]
        consumed = [arg_text]
        if name in _GLOBAL_VALUE_OPTIONS and "=" not in arg_text and index + 1 < len(argv):
            consumed.append(argv[index + 1])
        if name not in _GLOBAL_REPLAY_DROPPED:
            replay.extend(consumed)
        index += len(consumed)
    return GitInvocation(tuple(replay), None, ())


@dataclass(frozen=True)
class PushRequest:
    delete: bool = False
    all_branches: bool = False
    mirror: bool = False
    tags: bool = False
    repository: str | None = None
    refspecs: tuple[str, ...] = ()


def _resolve_long_option(name: str) -> tuple[str, bool] | None:
    if name in _PUSH_LONG_OPTIONS:
        return name, _PUSH_LONG_OPTIONS[name]
    matches = [known for known in _PUSH_LONG_OPTIONS if known.startswith(name)]
    return (matches[0], _PUSH_LONG_OPTIONS[matches[0]]) if len(matches) == 1 else None


@dataclass
class _PushScan:
    flags: set[str] = field(default_factory=set)
    repo_option: str | None = None
    positionals: list[str] = field(default_factory=list)


def _scan_long_option(args: Sequence[str], index: int, scan: _PushScan) -> int:
    name, has_value, value = args[index][2:].partition("=")
    resolved = None if name.startswith("no-") else _resolve_long_option(name)
    if resolved is None:
        return index + 1
    canonical, takes_value = resolved
    index += 1
    if takes_value and not has_value and index < len(args):
        value = args[index]
        index += 1
    scan.flags.add(canonical)
    if canonical == "repo":
        scan.repo_option = value
    return index


def _scan_short_options(args: Sequence[str], index: int, scan: _PushScan) -> int:
    arg_text = args[index]
    for position, letter in enumerate(arg_text[1:], start=1):
        if letter in _PUSH_SHORT_VALUE_OPTIONS:
            return index + (2 if position == len(arg_text) - 1 else 1)
        if letter == "d":
            scan.flags.add("delete")
    return index + 1


def parse_push(args: Sequence[str]) -> PushRequest:
    scan = _PushScan()
    options_ended = False
    index = 0
    while index < len(args):
        arg_text = args[index]
        if options_ended or not arg_text.startswith("-") or arg_text == "-":
            scan.positionals.append(arg_text)
            index += 1
        elif arg_text == "--":
            options_ended = True
            index += 1
        elif arg_text.startswith("--"):
            index = _scan_long_option(args, index, scan)
        else:
            index = _scan_short_options(args, index, scan)
    positionals = scan.positionals
    if scan.repo_option is not None:
        repository, refspecs = scan.repo_option, positionals
    else:
        repository, refspecs = (positionals[0] if positionals else None), positionals[1:]
    return PushRequest(
        delete="delete" in scan.flags,
        all_branches="all" in scan.flags or "branches" in scan.flags,
        mirror="mirror" in scan.flags,
        tags="tags" in scan.flags,
        repository=repository,
        refspecs=tuple(refspecs),
    )


def _normalized(url: str) -> str:
    if "://" in url:
        rest = url.split("://", 1)[1]
        host, _, path = rest.partition("/")
        host = host.rsplit("@", 1)[-1].split(":", 1)[0]
        return f"{host}/{path}"
    scp_like = re.fullmatch(r"(?:[^@/:]+@)?([^/:]+):(.*)", url)
    if scp_like is not None:
        return f"{scp_like.group(1)}/{scp_like.group(2).lstrip('/')}"
    return url


def matches_zero_trace_remote(urls: Sequence[str], patterns: str) -> bool:
    entries = [entry.strip().lower() for entry in patterns.split(",") if entry.strip()]
    candidates = [form.lower() for url in urls for form in (url, _normalized(url))]
    return any(
        fnmatch.fnmatchcase(candidate, f"*{entry}*")
        for entry in entries
        for candidate in candidates
    )


def self_agent_id(environ: Mapping[str, str]) -> int | None:
    raw = environ.get(AGENT_ID_ENV, "")
    if raw.isdecimal():
        return int(raw)
    handle = environ.get(HANDLE_ENV, "")
    if not handle:
        return None
    return _agent_id_of_handle(handle, environ)


def _agent_id_of_handle(handle: str, environ: Mapping[str, str]) -> int | None:
    raw_home = environ.get(HOME_ENV)
    home = Path(raw_home).expanduser() if raw_home else Path.home() / ".meiva"
    digest = hashlib.sha256(handle.encode("utf-8")).hexdigest()
    for path in sorted((home / "harness" / "registrations").glob("*.json")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if path.stem.isdecimal() and digest in text:
            return int(path.stem)
    return None


def branch_name_problem(name: str, agent_id: int | None) -> str | None:
    lowered = name.lower()
    for word in PLATFORM_WORDS:
        if word in lowered:
            return f"contains the platform word '{word}'"
    if agent_id is not None and re.search(rf"(?:^|[-_/.#]){agent_id}(?:$|[-_/.])", name):
        return f"contains this agent's id {agent_id}"
    return None


def commit_message_problem(message: str) -> str | None:
    for label, pattern in _TRAILER_PATTERNS:
        found = pattern.search(message)
        if found is not None:
            return f"carries {label} ({found.group(0).strip()!r})"
    return None


@dataclass(frozen=True)
class Violation:
    summary: str
    fix: str


@dataclass
class GitRunner:
    real_git: str
    replay: tuple[str, ...]
    environ: Mapping[str, str]

    def run(self, *args: str) -> str | None:
        try:
            done = subprocess.run(  # noqa: S603
                [self.real_git, *self.replay, *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=dict(self.environ),
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except OSError:
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    def lines(self, *args: str) -> list[str]:
        output = self.run(*args)
        return output.splitlines() if output else []

    def config(self, key: str) -> str | None:
        return self.run("config", "--get", key) or None


def _short_ref(name: str) -> str:
    for prefix in ("refs/heads/", "refs/tags/", "refs/"):
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name


@dataclass
class PushPlan:
    names: list[str] = field(default_factory=list)
    tips: list[str] = field(default_factory=list)


def _target_remote(git: GitRunner, request: PushRequest, branch: str | None) -> str:
    if request.repository:
        return request.repository
    keys = [*([f"branch.{branch}.pushRemote"] if branch else []), "remote.pushDefault"]
    if branch:
        keys.append(f"branch.{branch}.remote")
    for key in keys:
        value = git.config(key)
        if value:
            return value
    return "origin"


def _push_urls(git: GitRunner, remote: str) -> list[str]:
    configured = git.lines("remote", "get-url", "--push", "--all", remote)
    if configured:
        return configured
    expanded = git.run("ls-remote", "--get-url", remote)
    return [remote, expanded] if expanded and expanded != remote else [remote]


def _expand_glob_refspec(git: GitRunner, source: str, destination: str) -> list[tuple[str, str]]:
    prefix = source.split("*", 1)[0]
    pairs: list[tuple[str, str]] = []
    for ref in git.lines("for-each-ref", "--format=%(refname)", prefix):
        if fnmatch.fnmatchcase(ref, source):
            stem = ref[len(prefix) :]
            pairs.append((ref, destination.replace("*", stem, 1)))
    return pairs


def _plan_refspec(git: GitRunner, refspec: str, branch: str | None, plan: PushPlan) -> None:
    spec = refspec.removeprefix("+")
    source, colon, destination = spec.partition(":")
    if colon and not source:
        return
    if not destination:
        destination = source
    pairs = [(source, destination)]
    if "*" in source:
        pairs = _expand_glob_refspec(git, source, destination)
    for local, pushed_as in pairs:
        remote_ref = (branch or "") if local == pushed_as == "HEAD" else pushed_as
        if remote_ref:
            plan.names.append(_short_ref(remote_ref))
        plan.tips.append(local)


def _plan_default_push(git: GitRunner, remote: str, branch: str | None, plan: PushPlan) -> None:
    configured = git.lines("config", "--get-all", f"remote.{remote}.push")
    if configured:
        for refspec in configured:
            _plan_refspec(git, refspec, branch, plan)
        return
    if branch is None:
        return
    mode = git.config("push.default") or "simple"
    if mode == "nothing":
        return
    if mode == "matching":
        remote_branches = {
            _short_ref(ref).removeprefix(f"{remote}/")
            for ref in git.lines("for-each-ref", "--format=%(refname)", f"refs/remotes/{remote}/")
        }
        for local in git.lines("for-each-ref", "--format=%(refname:short)", "refs/heads/"):
            if local in remote_branches:
                plan.names.append(local)
                plan.tips.append(local)
        return
    name = branch
    if mode in ("upstream", "tracking"):
        merge = git.config(f"branch.{branch}.merge")
        name = _short_ref(merge) if merge else branch
    plan.names.append(name)
    plan.tips.append(branch)


def _ref_names(git: GitRunner, *patterns: str) -> list[str]:
    return [_short_ref(ref) for ref in git.lines("for-each-ref", "--format=%(refname)", *patterns)]


def _plan_ref_sets(git: GitRunner, request: PushRequest, plan: PushPlan) -> None:
    if request.mirror:
        plan.names.extend(_ref_names(git))
        plan.tips.append("--all")
    elif request.all_branches:
        plan.names.extend(_ref_names(git, "refs/heads/"))
        plan.tips.append("--branches")
    if request.tags:
        plan.names.extend(_ref_names(git, "refs/tags/"))
        plan.tips.append("--tags")


def _build_plan(git: GitRunner, request: PushRequest, remote: str, branch: str | None) -> PushPlan:
    plan = PushPlan()
    for refspec in request.refspecs:
        _plan_refspec(git, refspec, branch, plan)
    if request.all_branches or request.mirror or request.tags:
        _plan_ref_sets(git, request, plan)
    elif not request.refspecs:
        _plan_default_push(git, remote, branch, plan)
    return plan


def _pending_commits(git: GitRunner, remote: str, tips: Sequence[str]) -> list[tuple[str, str]]:
    has_tracking = bool(git.lines("for-each-ref", "--count=1", f"refs/remotes/{remote}/"))
    exclusion = ["--not", f"--remotes={remote}"] if has_tracking else []
    output = git.run(
        "log",
        "--no-color",
        "--no-show-signature",
        "--format=%x1e%H%x1f%B",
        f"--max-count={COMMIT_SCAN_LIMIT}",
        *tips,
        *exclusion,
    )
    commits: list[tuple[str, str]] = []
    for record in (output or "").split("\x1e"):
        sha, _, message = record.partition("\x1f")
        if sha.strip():
            commits.append((sha.strip(), message))
    return commits


def find_violations(
    git: GitRunner, request: PushRequest, environ: Mapping[str, str]
) -> tuple[str, list[Violation]]:
    branch = git.run("symbolic-ref", "--quiet", "--short", "HEAD")
    remote = _target_remote(git, request, branch)
    patterns = environ.get(REMOTES_ENV, "")
    if not matches_zero_trace_remote(_push_urls(git, remote), patterns):
        return remote, []
    plan = _build_plan(git, request, remote, branch)
    agent_id = self_agent_id(environ)
    violations: list[Violation] = []
    for name in dict.fromkeys(plan.names):
        problem = branch_name_problem(name, agent_id)
        if problem is not None:
            violations.append(
                Violation(
                    f"remote branch name '{name}' {problem}",
                    f"rename it: `git branch -m {name} <clean-name>` and push `<clean-name>`, "
                    f"or push the local ref to a clean name with `git push {remote} <local-ref>:<clean-name>`",
                )
            )
    tips = [tip for tip in dict.fromkeys(plan.tips) if not tip.startswith("-") or tip in _TIP_FLAGS]
    for sha, message in _pending_commits(git, remote, tips) if tips else []:
        problem = commit_message_problem(message)
        if problem is not None:
            violations.append(
                Violation(
                    f"commit {sha[:12]} {problem}",
                    f"rewrite the message: `git commit --amend` for the tip, or `git rebase -i {sha[:12]}~1` "
                    f"and reword it, then push again",
                )
            )
    return remote, violations


def _exec_git(real_git: str, args: Sequence[str]) -> None:
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(real_git, ["git", *args])  # noqa: S606


def main(argv: Sequence[str], environ: Mapping[str, str]) -> int:
    real_git, git_args = argv[1], list(argv[2:])
    try:
        invocation = parse_invocation(git_args)
        if invocation.subcommand == "push" and environ.get(REMOTES_ENV, "").strip():
            request = parse_push(invocation.args)
            if not request.delete:
                git = GitRunner(real_git, invocation.replay, environ)
                remote, violations = find_violations(git, request, environ)
                if violations:
                    lines = [f"git push to '{remote}' blocked by the host zero-trace guard:"]
                    for violation in violations:
                        lines.append(f"  - {violation.summary}")
                        lines.append(f"    fix: {violation.fix}")
                    lines.append(
                        "  Deleting a pushed branch is never blocked: `git push <remote> --delete <branch>`."
                    )
                    sys.stderr.write("\n".join(lines) + "\n")
                    return BLOCKED_EXIT_CODE
    except Exception as fault:
        sys.stderr.write(f"zero-trace guard fault, the push was not checked: {fault!r}\n")
    _exec_git(real_git, git_args)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv, os.environ))
