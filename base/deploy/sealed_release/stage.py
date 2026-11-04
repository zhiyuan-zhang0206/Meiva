# meiva:note
# The host-side steps of the release-directory layout: stage one commit of a full clone as a
# sealed release of a home, point the home's `source` at a release, and verify a release. The
# decisions are in `stage_plan.py`, `build_plan.py` and `sealed_plan.py`, the disk work of
# sealing in `sealed_tree.py`. Production does not switch to releases yet; the entry exists so
# one host can be staged, switched and verified by hand.
# meiva:link future/infra/lifecycle/release-dir-atomic-code-swap.md

from __future__ import annotations

import argparse
import functools
import io
import os
import subprocess
import sys
import tarfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import assert_never

from base.deploy.sealed_release.build_plan import build_env, compile_argv, sync_argv, utc_iso
from base.deploy.sealed_release.sealed_plan import ManifestError, manifest_path, report
from base.deploy.sealed_release.sealed_tree import (
    SealedTreeError,
    remove,
    seal,
    snapshot,
    verify,
    write_manifest,
)
from base.deploy.sealed_release.stage_plan import (
    RELEASES_DIRNAME,
    LinkKind,
    StageAction,
    StagePlanError,
    release_id,
    releases_dir,
    source_link,
    stage_action,
    switch_refusal,
)
from base.host.atomic_io import fsync_parent
from base.host.boundary.options import attribute, options
from base.host.boundary.process import stderr_bytes, stderr_text, stdout_bytes, stdout_text
from base.host.git.gitenv import git_env
from base.host.system.hostport import Host
from base.native_process.code_version import CodeVersionError, first_parent_count
from base.native_process.loaded_commit import RELEASE_RECORD, ReleaseRecord, ReleaseRecordError

_GIT_TIMEOUT_S = 120.0
_SYNC_TIMEOUT_S = 1800.0
_COMPILE_TIMEOUT_S = 600.0

# meiva:note
# Fills a release's build outputs after the archive and record are in place and before it is
# sealed: `build_python` in production, a cheaper stand-in in a test.
Build = Callable[[Path], None]


class StageError(RuntimeError):
    pass


# meiva:note
# The record of `commit` as this clone knows it. The first-parent count needs the full history,
# which is why it is computed here and carried, not recomputed from the release; a shallow clone
# would fix a count that is too low into an immutable record, so it is refused.
def release_record(repo: Path, commit: str, *, host: Host) -> ReleaseRecord:
    if _git(host, repo, "rev-parse", "--is-shallow-repository") != "false":
        raise StageError(f"{repo} is a shallow clone: its first-parent count would be too low")
    sha = _git(host, repo, "rev-parse", "--verify", f"{commit}^{{commit}}")
    committed = _git(host, repo, "show", "-s", "--format=%cI", sha)
    return ReleaseRecord(
        commit=sha,
        code_version=first_parent_count(repo, sha),
        committed_at=utc_iso(committed),
    )


def _git(host: Host, repo: Path, *args: str) -> str:
    done = host.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=git_env(host.environ),
        timeout=_GIT_TIMEOUT_S,
    )
    if done.returncode != 0:
        raise StageError(
            f"`git {' '.join(args)}` in {repo} exited {done.returncode}: {stderr_text(done)}"
        )
    return stdout_text(done).strip()


# meiva:note
# Stage `commit` under the home's releases and return the release. Rerunning is safe: a finished
# release that still verifies is returned as is, one that stopped part-way is rebuilt, and one
# that changed since sealing is refused for a person to look at.
def stage(repo: Path, commit: str, *, home: Path, host: Host, build: Build) -> Path:
    record = release_record(repo, commit, host=host)
    releases = releases_dir(home)
    releases.mkdir(exist_ok=True)
    release = releases.resolve(strict=True) / release_id(record.commit, record.code_version)
    manifest = manifest_path(release)
    action = stage_action(
        sealed=manifest.exists() and release.exists(),
        present=manifest.exists() or release.exists(),
        live=_live_release(home) == release,
    )
    match action:
        case StageAction.REUSE:
            return _unchanged(release, host=host)
        case StageAction.REBUILD:
            remove(release, host=host)
        case StageAction.BUILD:
            pass
        case _:
            assert_never(action)
    _extract(repo, record.commit, release, host=host)
    (release / RELEASE_RECORD).write_text(record.to_json(), encoding="utf-8")
    build(release)
    seal(release, host=host)
    write_manifest(release, snapshot(release), host=host)
    return release


# meiva:note
# A finished release is returned only if it still verifies. Its manifest is sealed again because a
# stage that stopped between publishing the manifest and sealing it leaves it writable; sealing is
# idempotent.
def _unchanged(release: Path, *, host: Host) -> Path:
    findings = verify(release, content=False)
    if findings:
        lines = "\n  ".join(report(findings))
        raise StageError(f"{release} changed since it was sealed:\n  {lines}")
    seal(manifest_path(release), host=host)
    return release


def _live_release(home: Path) -> Path | None:
    link = source_link(home)
    return link.resolve() if link.is_symlink() else None


# meiva:note
# The tracked tree of the commit, without `.git`. The `data` filter refuses absolute paths, links
# that leave the tree and device files.
def _extract(repo: Path, sha: str, release: Path, *, host: Host) -> None:
    done = host.run(
        ["git", "-C", str(repo), "archive", "--format=tar", sha],
        capture_output=True,
        env=git_env(host.environ),
        timeout=_GIT_TIMEOUT_S,
    )
    if done.returncode != 0:
        raise StageError(
            f"`git archive {sha}` in {repo} exited {done.returncode}: {stderr_bytes(done)!r}"
        )
    release.mkdir()
    with tarfile.open(fileobj=io.BytesIO(stdout_bytes(done))) as archive:
        archive.extractall(release, filter="data")


# meiva:note
# The release's own virtualenv, synced from its lock at the path it will run from (a venv does
# not move), then every source file compiled ahead of sealing.
def build_python(release: Path, *, host: Host) -> None:
    uv = host.which("uv")
    if uv is None:
        raise StageError("uv is not on PATH")
    env = build_env(host.environ)
    sync = sync_argv(uv, system=host.platform.system)
    _check(
        host.run(
            sync, cwd=release, env=env, capture_output=True, text=True, timeout=_SYNC_TIMEOUT_S
        ),
        argv=sync,
    )
    compile_all = compile_argv(release / ".venv" / "bin" / "python", release)
    _check(
        host.run(compile_all, env=env, capture_output=True, text=True, timeout=_COMPILE_TIMEOUT_S),
        argv=compile_all,
    )


def _check(done: subprocess.CompletedProcess[str], *, argv: Sequence[str]) -> None:
    if done.returncode != 0:
        tail = "\n".join(
            (stderr_text(done) or stdout_text(done) or "no output").strip().splitlines()[-5:]
        )
        raise StageError(f"`{' '.join(argv)}` exited {done.returncode}:\n{tail}")


# meiva:note
# Point the home's `source` at `release` with one rename over the old pointer, so a reader sees
# either release and never a missing `source`. The pointer is relative to the home.
def switch(home: Path, release: Path) -> Path:
    target = release.resolve(strict=True)
    own = target.parent == releases_dir(home).resolve(strict=True)
    findings = verify(target, content=False) if own else ()
    link = source_link(home)
    refusal = switch_refusal(link=_link_kind(link), own_release=own, findings=findings)
    if refusal is not None:
        raise StageError(f"refused to switch {link} to {target}: {refusal}")
    staged = link.with_name(f".{link.name}.next")
    staged.unlink(missing_ok=True)
    staged.symlink_to(Path(RELEASES_DIRNAME) / target.name)
    staged.replace(link)
    fsync_parent(link)
    return target


def _link_kind(link: Path) -> LinkKind:
    if link.is_symlink():
        return LinkKind.POINTER
    return LinkKind.OTHER if link.exists() else LinkKind.ABSENT


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m base.deploy.sealed_release.stage",
        description="Stage, switch to and verify sealed source releases of one home",
    )
    verbs = parser.add_subparsers(dest="verb", required=True)
    staging = verbs.add_parser("stage", help="stage a commit of a full clone as a sealed release")
    staging.add_argument("--repo", type=Path, required=True, help="a full clone holding the commit")
    staging.add_argument("--commit", required=True)
    staging.add_argument("--home", type=Path, required=True)
    switching = verbs.add_parser(
        "switch",
        help="point a scratch home's source at a sealed release (phase 1: a production home "
        "still needs its checkout for the fleet update, drift reads and the frontend)",
    )
    switching.add_argument("--home", type=Path, required=True)
    switching.add_argument("--release", type=Path, required=True)
    checking = verbs.add_parser("verify", help="compare a release with its manifest")
    checking.add_argument("--release", type=Path, required=True)
    checking.add_argument("--content", action="store_true", help="also rehash every file")
    return parser


# meiva:note
# The options of each verb, read from the parsed namespace by `options` (each verb sets its own
# subset, so there is one shape per verb).
@dataclass(frozen=True)
class _StageOptions:
    repo: Path
    commit: str
    home: Path


@dataclass(frozen=True)
class _SwitchOptions:
    home: Path
    release: Path


@dataclass(frozen=True)
class _VerifyOptions:
    release: Path
    content: bool


def _run_verb(args: argparse.Namespace, *, host: Host) -> int:
    verb = attribute(args, "verb", str)
    match verb:
        case "stage":
            chosen = options(args, _StageOptions)
            build: Build = functools.partial(build_python, host=host)
            staged = stage(chosen.repo, chosen.commit, home=chosen.home, host=host, build=build)
            sys.stdout.write(f"{staged}\n")
            return 0
        case "switch":
            switching = options(args, _SwitchOptions)
            sys.stdout.write(f"{switch(switching.home, switching.release)}\n")
            return 0
        case "verify":
            checking = options(args, _VerifyOptions)
            findings = verify(checking.release, content=checking.content)
            sys.stdout.writelines(f"{line}\n" for line in report(findings))
            return 1 if findings else 0
        case _:
            raise ValueError(f"unknown verb {verb!r}")


_FAILURES = (
    StageError,
    StagePlanError,
    SealedTreeError,
    ManifestError,
    ReleaseRecordError,
    CodeVersionError,
    OSError,
)


def main(argv: Sequence[str], *, host: Host) -> int:
    args = _parser().parse_args(argv)
    try:
        return _run_verb(args, host=host)
    except _FAILURES as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], host=Host.system(environ=os.environ)))
