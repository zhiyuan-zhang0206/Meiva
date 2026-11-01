"""Decide which CI lanes a pull request needs (the `classify` job): frontend, backend, or both.

`python3 -m scripts.ci.pull_requests.classify_paths <scratch dir> <head ref>`, from the repository root. The
scratch directory holds `changed.txt` (one changed path per line) and `base.txt` (the merge base). It
prints `frontend=true|false` and `backend=true|false`, the lines the job appends to `$GITHUB_OUTPUT`.
"""

import sys
from pathlib import Path

from base.deploy.git.repo_change import classify_change
from scripts.ci.test_selector import documentation_runtime_tests


def main(scratch: Path, _head_ref: str = "", *, repo_root: Path | None = None) -> None:
    paths = (scratch / "changed.txt").read_text().splitlines()
    frontend, backend = classify_change(paths)
    if not backend:
        base_ref = (scratch / "base.txt").read_text().strip() or None
        backend = bool(
            documentation_runtime_tests(
                paths,
                repo_root=Path.cwd() if repo_root is None else repo_root,
                base_ref=base_ref,
            )
        )
    sys.stdout.write(f"frontend={str(frontend).lower()}\nbackend={str(backend).lower()}\n")


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else "")
