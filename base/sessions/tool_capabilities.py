# meiva:note
# A launcher depends on flags that drift between CLI releases, so the dependency is declared here
# once instead of living as prose in a skill reference. The table states what we require and the
# evidence behind each floor; the probe states what this machine actually has. Version and flag
# probes are the only commands run: authentication is never touched, because a probe must not be a
# setup side effect. A machine without the binary is `unknown`, never "not installed": the binary
# may simply sit outside PATH, as it does on a non-interactive ssh session.
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from base.host.proc import run_bounded
from base.log import logger
from base.paths import meiva_home

PROBE_TTL_SECONDS = 24 * 3600
PROBE_TIMEOUT_SECONDS = 3.0
_VERSION = re.compile(r"\d+\.\d+\.\d+")

Version = tuple[int, int, int]
Runner = Callable[[list[str]], tuple[int, str]]


@dataclass(frozen=True)
class Evidence:
    verified_on: str
    machine: str
    version: str


@dataclass(frozen=True)
class Feature:
    name: str
    marker: str
    fallback: str
    help_argv: tuple[str, ...] = ("--help",)
    min_version: Version | None = None
    evidence: Evidence | None = None


@dataclass(frozen=True)
class ToolSpec:
    tool: str
    candidates: tuple[str, ...]
    features: tuple[Feature, ...]
    version_argv: tuple[str, ...] = ("--version",)


_CLAUDE_STREAM = Evidence("2026-10-18", "five hosts", "2.1.283-2.1.290")
_CODEX_JSON = Evidence("2026-10-18", "five hosts", "0.155.1-0.163.0")
_CODEX_SERVER = Evidence("2026-10-18", "T3 Code floor", "0.159")

TOOLS: dict[str, ToolSpec] = {
    "claude": ToolSpec(
        tool="claude",
        candidates=(
            "~/.local/bin/claude",
            "/usr/local/bin/claude",
            "/opt/homebrew/bin/claude",
        ),
        features=(
            Feature(
                name="stream_json_out",
                marker="--output-format",
                fallback="pty",
                min_version=(2, 1, 283),
                evidence=_CLAUDE_STREAM,
            ),
            Feature(
                name="stream_json_in",
                marker="--input-format",
                fallback="pty",
                min_version=(2, 1, 283),
                evidence=_CLAUDE_STREAM,
            ),
            Feature(name="resume", marker="--resume", fallback="fresh-session"),
        ),
    ),
    "codex": ToolSpec(
        tool="codex",
        candidates=("~/.local/bin/codex", "/usr/local/bin/codex", "/opt/homebrew/bin/codex"),
        features=(
            Feature(
                name="exec_json",
                marker="--json",
                help_argv=("exec", "--help"),
                fallback="pty",
                min_version=(0, 155, 1),
                evidence=_CODEX_JSON,
            ),
            Feature(
                name="app_server",
                marker="app-server",
                fallback="pty",
                min_version=(0, 159, 0),
                evidence=_CODEX_SERVER,
            ),
        ),
    ),
}


@dataclass(frozen=True)
class Probe:
    tool: str
    path: str | None
    version: Version | None
    version_text: str | None
    flags: dict[str, bool] = field(default_factory=dict[str, bool])
    unknown_reason: str | None = None
    probed_at: dt.datetime | None = None

    def state(self, feature: Feature) -> str:
        if self.unknown_reason is not None:
            return "unknown"
        if not self.flags.get(feature.name, False):
            return "below_floor"
        if feature.min_version is not None and (
            self.version is None or self.version < feature.min_version
        ):
            return "present_untested"
        return "verified"


@dataclass(frozen=True)
class Decision:
    mode: str
    refused: bool
    reasons: tuple[str, ...]


def parse_version(text: str) -> Version | None:
    match = _VERSION.search(text)
    if match is None:
        return None
    major, minor, patch = match[0].split(".")
    return int(major), int(minor), int(patch)


def _default_run(argv: list[str]) -> tuple[int, str]:
    try:
        completed = run_bounded(argv, timeout=PROBE_TIMEOUT_SECONDS, capture_output=True, text=True)
    except Exception as exc:
        logger.opt(exception=True).warning("tool probe failed: {}", argv)
        return -1, f"{type(exc).__name__}: {exc}"
    return completed.returncode, f"{completed.stdout or ''}{completed.stderr or ''}"


def _resolve(spec: ToolSpec) -> str | None:
    found = shutil.which(spec.tool)
    if found:
        return found
    for candidate in spec.candidates:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def cache_path() -> Path:
    return meiva_home() / "host" / "tool-probes.json"


def _cache_key(path: str) -> str:
    try:
        stat = Path(path).stat()
    except OSError:
        return hashlib.sha256(path.encode()).hexdigest()
    identity = f"{Path(path).resolve()}|{stat.st_mtime_ns}|{stat.st_size}"
    return hashlib.sha256(identity.encode()).hexdigest()


def _read_cache(target: Path) -> dict[str, object]:
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return cast("dict[str, object]", payload) if isinstance(payload, dict) else {}


def _write_cache(target: Path, payload: dict[str, object]) -> None:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        temporary.replace(target)
        target.chmod(0o600)
    except OSError:
        logger.opt(exception=True).warning("tool probe cache is not writable: {}", target)


def _from_cache(entry: object, *, now: dt.datetime) -> Probe | None:
    if not isinstance(entry, dict):
        return None
    record = cast("dict[str, object]", entry)
    probed_at = record.get("probed_at")
    if not isinstance(probed_at, str):
        return None
    try:
        stamped = dt.datetime.fromisoformat(probed_at)
    except ValueError:
        return None
    if now - stamped.astimezone(dt.UTC) > dt.timedelta(seconds=PROBE_TTL_SECONDS):
        return None
    version_text = record.get("version_text")
    version = parse_version(version_text) if isinstance(version_text, str) else None
    flags = record.get("flags")
    known_flags: dict[str, bool] = {}
    if isinstance(flags, dict):
        known_flags = {
            str(key): bool(value) for key, value in cast("dict[object, object]", flags).items()
        }
    path = record.get("path")
    unknown_reason = record.get("unknown_reason")
    return Probe(
        tool=str(record.get("tool", "")),
        path=path if isinstance(path, str) else None,
        version=version,
        version_text=version_text if isinstance(version_text, str) else None,
        flags=known_flags,
        unknown_reason=unknown_reason if isinstance(unknown_reason, str) else None,
        probed_at=stamped,
    )


def _to_cache(probe: Probe) -> dict[str, object]:
    return {
        "tool": probe.tool,
        "path": probe.path,
        "version_text": probe.version_text,
        "flags": probe.flags,
        "unknown_reason": probe.unknown_reason,
        "probed_at": (probe.probed_at or dt.datetime.now(dt.UTC)).isoformat(),
    }


# meiva:note
# A cached probe is keyed by the binary's real path, mtime and size, and expires after a day, so an
# upgraded CLI is probed again while a launch pays no subprocess. Unknown results are cached too:
# otherwise every launch on a host without the tool would wait out the probe timeout again.
def probe_tool(
    spec: ToolSpec,
    *,
    run: Runner | None = None,
    store: Path | None = None,
    now: dt.datetime | None = None,
) -> Probe:
    runner = run or _default_run
    target = store or cache_path()
    resolved = _resolve(spec)
    if resolved is None:
        return Probe(
            tool=spec.tool,
            path=None,
            version=None,
            version_text=None,
            unknown_reason="not found on PATH or its candidate paths",
            probed_at=now or dt.datetime.now(dt.UTC),
        )
    reference = now or dt.datetime.now(dt.UTC)
    key = _cache_key(resolved)
    cache = _read_cache(target)
    cached = _from_cache(cache.get(key), now=reference)
    if cached is not None and cached.tool == spec.tool:
        return cached

    code, version_text = runner([resolved, *spec.version_argv])
    version = parse_version(version_text) if code == 0 else None
    flags: dict[str, bool] = {}
    help_cache: dict[tuple[str, ...], str] = {}
    for feature in spec.features:
        help_argv = feature.help_argv
        if help_argv not in help_cache:
            help_code, help_text = runner([resolved, *help_argv])
            help_cache[help_argv] = help_text if help_code == 0 else ""
        flags[feature.name] = feature.marker in help_cache[help_argv]
    result = Probe(
        tool=spec.tool,
        path=resolved,
        version=version,
        version_text=version_text.strip() or None,
        flags=flags,
        unknown_reason=None if version is not None else "version output was not a version",
        probed_at=reference,
    )
    cache[key] = _to_cache(result)
    _write_cache(target, cache)
    return result


# meiva:note
# Only one execution path exists today, so the decision is honest about it: an explicit request for
# a path this build does not implement is refused, while every capability shortfall degrades to the
# path that does exist and says why. A probe that failed is a degradation, never a refusal, because
# a launch must not depend on a subprocess this module did not get to run.
def decide(
    spec: ToolSpec,
    *,
    requested: str = "auto",
    force: bool = False,
    probe: Probe | None = None,
    run: Runner | None = None,
    store: Path | None = None,
) -> Decision:
    state = probe or probe_tool(spec, run=run, store=store)
    implemented = {"pty"}
    if requested not in {"auto", *implemented}:
        return Decision(
            mode=requested,
            refused=True,
            reasons=(f"{requested} is not implemented in this build; use --mode pty or auto",),
        )
    reasons: list[str] = []
    if state.unknown_reason is not None:
        reasons.append(f"probe unknown ({state.unknown_reason}); using pty")
    for feature in spec.features:
        feature_state = state.state(feature)
        if feature_state == "verified":
            continue
        if feature_state == "unknown":
            continue
        if force:
            reasons.append(f"{feature.name}: {feature_state} on {state.version_text}; forced")
            continue
        reasons.append(
            f"{feature.name}: {feature_state} on {state.version_text}; {feature.fallback}"
        )
    return Decision(mode="pty", refused=False, reasons=tuple(reasons))


def render(tool: str, decision: Decision, probe: Probe) -> str:
    version = ".".join(str(part) for part in probe.version) if probe.version else "unknown"
    features = ",".join(sorted(name for name, present in probe.flags.items() if present)) or "-"
    reasons = f" ({'; '.join(decision.reasons)})" if decision.reasons else ""
    where = probe.path or probe.unknown_reason or "missing"
    return f"harness: {tool}={version} [{where}] features=[{features}] decision={decision.mode}{reasons}"
