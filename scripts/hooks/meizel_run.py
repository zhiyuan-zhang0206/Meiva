from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from functools import partial
from pathlib import Path

import meizel
from meizel.api import GitRepo, MeizelError, tracked_files
from meizel.installation import typescript_toolchain

from scripts.hooks.diff_checks_cache import (
    CACHE_FILENAME,
    CacheContext,
    full_meizel_argv,
    lookup_run,
    meizel_full_report,
    meizel_pass,
    meizel_run_key,
    record_run,
)
from scripts.hooks.diff_checks_fields import meizel_field


def child_environment(parent: Mapping[str, str]) -> dict[str, str]:
    return {name: value for name, value in parent.items() if not name.startswith("MEIVA_")}


def git(root: Path, *argv: str) -> str:
    return subprocess.check_output(["git", *argv], cwd=root, text=True).strip()  # noqa: S603


def _file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _checkout_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for name in sorted(set(tracked_files(root, include_untracked=True, regular=True))):
        digest.update(name.encode() + b"\0")
        digest.update(hashlib.sha256((root / name).read_bytes()).digest())
    return digest.hexdigest()


def index_scan_problem(root: Path) -> str | None:
    try:
        repo = GitRepo(root)
        algorithm = git(root, "rev-parse", "--show-object-format")
        dirty = set(repo.run_bytes("diff-files", "--name-only", "-z", "--").split(b"\0"))
        regular = set(tracked_files(root, regular=True))
        for record in repo.run_bytes("ls-files", "--stage", "-z").split(b"\0"):
            if not record:
                continue
            header, name = record.split(b"\t", 1)
            _, indexed, stage = header.split()
            path = name.decode()
            if stage != b"0" or name in dirty or path not in regular:
                continue
            data = (root / path).read_bytes()
            digest = hashlib.new(algorithm, f"blob {len(data)}\0".encode())
            digest.update(data)
            if digest.hexdigest().encode() != indexed:
                return (
                    f"meizel: working-tree bytes differ from the index for {path!r}, "
                    "which git diff-files reports as clean. Update the file mtime and "
                    "stage the intended edit before retrying."
                )
    except (OSError, ValueError, subprocess.SubprocessError, MeizelError) as error:
        return f"meizel: could not verify scan input against the Git index: {error}"
    return None


def git_executable_digest(path: str) -> str:
    executable = shutil.which("git", path=path)
    if executable is None:
        raise FileNotFoundError("Git executable is missing")
    return _file_digest(Path(executable))


def _toolchain(root: Path, python: str, path: str) -> list[tuple[str, str]]:
    node = shutil.which("node", path=path)
    if node is None:
        raise FileNotFoundError("Node executable is missing")
    installed_meizel = Path(meizel.__file__).parent
    frontend = typescript_toolchain()
    compiler = frontend / "node_modules/typescript"
    required = (
        root / python,
        Path(node),
        frontend / "pnpm-lock.yaml",
        frontend / "node_modules/.modules.yaml",
        frontend / "node_modules/.pnpm/lock.yaml",
        compiler / "package.json",
        compiler / "lib/typescript.js",
    )
    checker = sorted(
        path for path in installed_meizel.rglob("*") if path.suffix in {".py", ".mjs", ".toml"}
    )
    return [(str(path.resolve(strict=True)), _file_digest(path)) for path in (*required, *checker)]


_GIT_PATH_QUERIES = {
    "GIT_EXEC_PATH": ("--exec-path",),
    "GIT_DIR": ("rev-parse", "--absolute-git-dir"),
}


def _default_git_paths(root: Path, environment: Mapping[str, str]) -> bool:
    clean = {key: value for key, value in environment.items() if key not in _GIT_PATH_QUERIES}
    for name, query in _GIT_PATH_QUERIES.items():
        if (actual := environment.get(name)) is None:
            continue
        expected = subprocess.check_output(  # noqa: S603
            ["git", *query],
            cwd=root,
            text=True,
            env=clean,
        ).strip()
        if (root / actual).resolve() != Path(expected).resolve():
            return False
    return True


def safe_environment(root: Path, environment: Mapping[str, str]) -> bool:
    if environment.get("GIT_PREFIX") or any(
        key
        in {
            "PYTHONHOME",
            "PYTHONPATH",
            "PYTHONPYCACHEPREFIX",
            "NODE_PATH",
            "NODE_OPTIONS",
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
            "DYLD_INSERT_LIBRARIES",
            "DYLD_LIBRARY_PATH",
        }
        or (
            key.startswith("GIT_")
            and key not in _GIT_PATH_QUERIES
            and key not in {"GIT_PAGER", "GIT_PREFIX", "GIT_TERMINAL_PROMPT"}
        )
        for key in environment
    ):
        return False
    return _default_git_paths(root, environment)


def _cacheable_checkout(root: Path, head: str) -> bool:
    if git(root, "rev-parse", "HEAD") != head:
        return False
    if git(root, "status", "--porcelain", "--untracked-files=normal", "--ignore-submodules=none"):
        return False
    if git(root, "for-each-ref", "--count=1", "refs/replace"):
        return False
    tracked = git(root, "ls-files", "-v", "-z").split("\0")
    return all(not entry or entry.startswith("H ") for entry in tracked)


def git_config_identity(root: Path) -> tuple[str, ...]:
    entries = git(root, "config", "--null", "--list").split("\0")
    identity: list[str] = []
    for entry in entries:
        key = entry.partition("\n")[0]
        if not (key.startswith("branch.") and key.endswith((".remote", ".merge"))):
            identity.append(entry)
    return tuple(identity)


def input_fingerprint(
    root: Path, head: str, *, python: str, environment: Mapping[str, str]
) -> str | None:
    try:
        if not safe_environment(root, environment) or not _cacheable_checkout(root, head):
            return None
        inputs = (
            str(root),
            _checkout_digest(root),
            git_config_identity(root),
            git(root, "--exec-path"),
            git_executable_digest(environment.get("PATH", "")),
            _toolchain(root, python, environment.get("PATH", "")),
        )
    except (OSError, RuntimeError, subprocess.SubprocessError, MeizelError, UnicodeError):
        return None
    return hashlib.sha256(json.dumps(inputs).encode()).hexdigest()


def _open_push_cache(
    root: Path, *, python: str, environment: Mapping[str, str], pushed: str
) -> CacheContext | None:
    try:
        head = git(root, "rev-parse", "HEAD")
        if pushed and pushed != head:
            return None
        tree = git(root, "rev-parse", f"{head}^{{tree}}")
        common_dir = Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    except (OSError, subprocess.CalledProcessError):
        return None
    return CacheContext(
        cache_path=common_dir / CACHE_FILENAME,
        head_tree=tree,
        base_sha="",
        python_digest="",
        meizel_input=partial(input_fingerprint, root, head, python=python, environment=environment),
    )


def cached_full_meizel(context: CacheContext, key: str) -> dict[str, str] | None:
    cached = lookup_run(context, key)
    if (
        cached is None
        or cached.code != 0
        or cached.fields is None
        or not meizel_pass(cached.fields)
    ):
        return None
    return cached.fields


def main(root: Path, *, python: str, environment: Mapping[str, str], pushed: str) -> int:
    argv = full_meizel_argv(python)
    cache = (
        None
        if environment.get("PRE_COMMIT") and not pushed
        else _open_push_cache(root, python=python, environment=environment, pushed=pushed)
    )
    key = meizel_run_key(cache, "meizel", argv) if cache is not None else None
    if cache is not None and key is not None:
        cached = cached_full_meizel(cache, key)
        if cached is not None:
            print(f"pre-push: reused full meizel check {cached['meizel']}")
            return 0

    if (problem := index_scan_problem(root)) is not None:
        print(problem, file=sys.stderr)
        return 1

    ran = subprocess.run(  # noqa: S603
        argv, cwd=root, env=dict(environment), capture_output=True, text=True, check=False
    )
    _ = sys.stdout.write(ran.stdout)
    _ = sys.stderr.write(ran.stderr)
    fields = {"meizel": meizel_field(ran.returncode, ran.stdout, 0.0)}
    if (
        cache is not None
        and key is not None
        and ran.returncode == 0
        and meizel_full_report(ran.stdout)
        and meizel_pass(fields)
        and meizel_run_key(cache, "meizel", argv) == key
    ):
        record_run(cache, key, code=0, output=ran.stdout + ran.stderr, fields=fields)
    return ran.returncode


if __name__ == "__main__":
    raise SystemExit(
        main(
            Path.cwd(),
            python=sys.executable,
            environment=child_environment(os.environ),
            pushed=os.environ.get("PRE_COMMIT_TO_REF", ""),
        )
    )
