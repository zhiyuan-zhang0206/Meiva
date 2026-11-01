#!/usr/bin/env python3
# meiva:note
# Keep the committed pnpm lock and workspace config on the public registry.
#
# Run without project dependencies in CI and through pre-commit (`python3 -S`).
# `pnpm install --frozen-lockfile` fetches through `pnpm_config_registry` at
# install time when a host mirror profile is active, while the committed
# `ui/web/pnpm-lock.yaml` and `ui/web/pnpm-workspace.yaml` must never record a
# mirror URL or registry override.

from __future__ import annotations

import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REGISTRY = "https://registry.npmjs.org/"
_URL_RE = re.compile(r"https?://[^\s'\"{}]+")


def _unquote(raw: str) -> str:
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def _noncanonical_url_errors(text: str) -> list[str]:
    errors: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        for url in _URL_RE.findall(line):
            if not url.startswith(_REGISTRY):
                errors.append(f"line {lineno}: URL {url} must start with {_REGISTRY}")
    return errors


def _lock_section_errors(active: list[str], text: str) -> list[str]:
    top_keys = {line.strip() for line in active if not line.startswith(" ")}
    errors: list[str] = []
    if "importers:" not in top_keys:
        errors.append("missing importers: section")
    if "packages:" not in top_keys:
        errors.append("missing packages: section")
    if "integrity: " not in text:
        errors.append("missing package integrity hashes")
    return errors


# meiva:note
# Report structural or non-canonical-registry violations in `pnpm-lock.yaml`.
def violations(path: Path) -> list[str]:
    if not path.is_file():
        return ["missing lockfile"]
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ["lockfile is not valid UTF-8"]
    active = [
        line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]
    if not active or not active[0].startswith("lockfileVersion:"):
        return ["expected lockfileVersion: '9.0' header"]
    if _unquote(active[0].partition(":")[2]) != "9.0":
        return ["expected lockfileVersion: '9.0' header"]
    return [*_lock_section_errors(active, text), *_noncanonical_url_errors(text)]


# meiva:note
# Report registry overrides or non-canonical URLs in `pnpm-workspace.yaml`.
def workspace_violations(path: Path) -> list[str]:
    if not path.is_file():
        return ["missing pnpm-workspace.yaml"]
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ["pnpm-workspace.yaml is not valid UTF-8"]
    errors: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, _ = stripped.partition(":")
        if sep and "registry" in _unquote(key).lower():
            errors.append(
                f"line {lineno}: registry key {key.strip()!r} belongs in host transport, not repo config"
            )
    errors.extend(_noncanonical_url_errors(text))
    return errors


def repo_violations(root: Path = _REPO_ROOT) -> list[str]:
    fe = root / "ui" / "web"
    errors = [f"ui/web/pnpm-lock.yaml: {err}" for err in violations(fe / "pnpm-lock.yaml")]
    errors.extend(
        f"ui/web/pnpm-workspace.yaml: {err}"
        for err in workspace_violations(fe / "pnpm-workspace.yaml")
    )
    for forbidden in ("package-lock.json", ".npmrc"):
        if (fe / forbidden).exists():
            errors.append(
                f"ui/web/{forbidden}: obsolete npm file present; ui/web uses pnpm-lock.yaml and pnpm-workspace.yaml"
            )
    return errors


def main() -> int:
    errors = repo_violations(_REPO_ROOT)
    for error in errors[:10]:
        print(error, file=sys.stderr)
    if errors:
        print(
            f"{len(errors)} frontend lock/config violation(s). Keep mirror settings to host "
            "transport and add or regenerate dependencies against registry.npmjs.org.",
            file=sys.stderr,
        )
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
