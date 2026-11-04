"""Unit manifests and the tree registry (K2 contract).

The root supervisor owns a tree of long-lived processes; the tree's shape is a
direct result of the declared unit manifests — there are no implicit members.

This module is deliberately data-only: it parses manifest files, validates
every field fail-fast (the K2 field set is closed, so an unknown field is an
error rather than a silently ignored field), and lays the tree out. Nothing
here spawns or stops anything.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from base.deploy.progress_timeout import ROOT_HEALTH_INTERVAL_S
from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_of
from services.supervision.meiva_root.inputs import InputSeal, parse_inputs

# The implicit tree root. Units attach here by default; it is never a unit
# itself and cannot be declared as one.
ROOT_ID = "root"

# Unit ids are lowercase slugs: stable, filesystem-safe, log-safe. `\Z`, not
# `$`: `$` also matches before a trailing newline, letting "gateway\n" through.
_UNIT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*\Z")

# The closed K2 field sets. A new field must go through design first — the
# validators reject anything else, so field drift fails loudly instead of
# being carried around silently.
_REQUIRED_FIELDS = frozenset({"id", "exec", "restart"})
_MANIFEST_FIELDS = _REQUIRED_FIELDS | {"attach", "env", "inputs", "stop_timeout_s", "stop_tier"}
_FILE_FIELDS = frozenset({"units", "launch_digest"})


class ManifestError(ValueError):
    """A manifest file or a unit manifest is malformed (fail-fast; no fallback)."""


class UnknownUnitError(LookupError):
    """The requested unit id is not part of the registry."""


class RestartPolicy(StrEnum):
    """When the supervisor restarts a unit after its process exits."""

    ALWAYS = "always"
    ON_FAILURE = "on-failure"
    NEVER = "never"


@dataclass(frozen=True, slots=True)
class UnitManifest:
    """One declared unit (K2): the minimal, closed field set."""

    id: str
    exec: tuple[str, ...]
    restart: RestartPolicy
    attach: str
    env: tuple[tuple[str, str], ...] = ()
    inputs: tuple[InputSeal, ...] = ()
    stop_timeout_s: float | None = None
    """Seconds root waits for this unit to exit after TERM; None = root's default.

    A unit whose own SIGTERM cleanup can outlast root's default declares it here,
    so root never gives up on a unit that is still closing inside its own bound."""
    # meiva:note
    # Nonnegative stop-wave tier among siblings at the same attach level (0 stops first).
    stop_tier: int = 1

    def digest(self) -> str:
        """Bind executable and private environment without exposing credentials."""
        body = json.dumps(
            {
                "id": self.id,
                "exec": self.exec,
                "restart": self.restart.value,
                "attach": self.attach,
                "env": self.env,
                "inputs": [item.as_mapping() for item in self.inputs],
                "stop_timeout_s": self.stop_timeout_s,
                "stop_tier": self.stop_tier,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(body.encode()).hexdigest()

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object], *, origin: str) -> UnitManifest:
        """Validate one manifest mapping; every deviation raises ManifestError."""
        unknown = set(raw) - _MANIFEST_FIELDS
        if unknown:
            raise ManifestError(
                f"{origin}: unknown manifest field(s) {sorted(unknown)}; the K2 field set is "
                "closed — extend it through design first"
            )
        missing = _REQUIRED_FIELDS - set(raw)
        if missing:
            raise ManifestError(f"{origin}: missing required field(s) {sorted(missing)}")

        unit_id = _require_str(raw, "id", origin)
        if unit_id == ROOT_ID:
            raise ManifestError(f"{origin}: id {ROOT_ID!r} is reserved for the tree root")
        if not _UNIT_ID_RE.match(unit_id):
            raise ManifestError(
                f"{origin}: id {unit_id!r} must be a lowercase slug "
                "(start with [a-z0-9], then [a-z0-9._-])"
            )

        restart_raw = _require_str(raw, "restart", origin)
        try:
            restart = RestartPolicy(restart_raw)
        except ValueError as exc:
            choices = [policy.value for policy in RestartPolicy]
            raise ManifestError(
                f"{origin}: restart {restart_raw!r} is not one of {choices}"
            ) from exc

        attach = raw.get("attach", ROOT_ID)
        if not isinstance(attach, str):
            raise ManifestError(f"{origin}: attach must be a string")
        if attach != ROOT_ID and not _UNIT_ID_RE.match(attach):
            raise ManifestError(f"{origin}: attach {attach!r} is not a valid unit id")

        try:
            inputs = parse_inputs(raw.get("inputs", []))
        except (ValueError, TypeError) as exc:
            raise ManifestError(f"{origin}: {exc}") from exc
        return cls(
            id=unit_id,
            exec=_parse_exec(raw["exec"], origin),
            restart=restart,
            attach=attach,
            env=_parse_environment(raw.get("env", {}), origin),
            inputs=inputs,
            stop_timeout_s=_parse_stop_timeout(raw.get("stop_timeout_s"), origin),
            stop_tier=_parse_stop_tier(raw.get("stop_tier"), origin),
        )


def _parse_exec(raw: object, origin: str) -> tuple[str, ...]:
    if not is_object_list(raw) or not raw:
        raise ManifestError(f"{origin}: exec must be a non-empty argv list")
    argv: list[str] = []
    for index, part in enumerate(raw):
        if not isinstance(part, str) or not part:
            raise ManifestError(f"{origin}: exec[{index}] must be a non-empty string")
        argv.append(part)
    return tuple(argv)


def _parse_stop_timeout(raw: object, origin: str, field: str = "stop_timeout_s") -> float | None:
    if raw is None:
        return None
    if (
        isinstance(raw, bool)
        or not isinstance(raw, (int, float))
        or not math.isfinite(raw)
        or raw <= 0
    ):
        raise ManifestError(f"{origin}: {field} must be a positive finite number of seconds")
    if field == "stop_timeout_s" and raw > ROOT_HEALTH_INTERVAL_S:
        raise ManifestError(
            f"{origin}: stop_timeout_s must not exceed the root health interval "
            f"({ROOT_HEALTH_INTERVAL_S:g}s) — a replacement stop runs inside a health round"
        )
    return float(raw)


def _parse_stop_tier(raw: object, origin: str) -> int:
    if raw is None:
        return 1
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ManifestError(f"{origin}: stop_tier must be a nonnegative integer")
    return raw


def _parse_environment(raw: object, origin: str) -> tuple[tuple[str, str], ...]:
    if not is_object_dict(raw):
        raise ManifestError(f"{origin}: env must contain valid string keys and values")
    pairs: list[tuple[str, str]] = []
    for key, value in raw.items():
        if (
            not isinstance(key, str)
            or not key
            or "=" in key
            or "\0" in key
            or not isinstance(value, str)
            or "\0" in value
        ):
            raise ManifestError(f"{origin}: env must contain valid string keys and values")
        pairs.append((key, value))
    return tuple(sorted(pairs))


def _require_str(raw: Mapping[str, object], field: str, origin: str) -> str:
    value = raw[field]
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{origin}: {field} must be a non-empty string")
    return value


class UnitRegistry:
    """The tree shape, derived directly from a closed set of manifests.

    Construction validates the whole forest once: duplicate ids, attachments to
    unknown units, and attach cycles all raise ManifestError.
    """

    def __init__(
        self, manifests: Sequence[UnitManifest], *, launch_digest: str | None = None
    ) -> None:
        if launch_digest is not None and not re.fullmatch(r"[0-9a-f]{64}", launch_digest):
            raise ManifestError("launch_digest must be a lowercase SHA256")
        self.launch_digest = launch_digest
        by_id: dict[str, UnitManifest] = {}
        for manifest in manifests:
            if manifest.id in by_id:
                raise ManifestError(f"duplicate unit id {manifest.id!r}")
            by_id[manifest.id] = manifest
        children: dict[str, list[str]] = {}
        for manifest in manifests:
            if manifest.attach != ROOT_ID and manifest.attach not in by_id:
                raise ManifestError(
                    f"unit {manifest.id!r} attaches to unknown unit {manifest.attach!r}"
                )
            children.setdefault(manifest.attach, []).append(manifest.id)
        self._validate_acyclic(by_id)
        self._manifests = by_id
        self._children = {parent: tuple(kids) for parent, kids in children.items()}

    @staticmethod
    def _validate_acyclic(by_id: Mapping[str, UnitManifest]) -> None:
        """Walk every unit's attach chain; a revisited link is a cycle."""
        for manifest in by_id.values():
            seen: set[str] = set()
            cursor = manifest.id
            while cursor != ROOT_ID:
                if cursor in seen:
                    raise ManifestError(f"attach cycle reached from unit {manifest.id!r}")
                seen.add(cursor)
                cursor = by_id[cursor].attach

    def get(self, unit_id: str) -> UnitManifest:
        """Return the manifest for `unit_id`; UnknownUnitError when absent."""
        try:
            return self._manifests[unit_id]
        except KeyError:
            raise UnknownUnitError(f"unknown unit {unit_id!r}") from None

    @property
    def units(self) -> tuple[UnitManifest, ...]:
        """Every unit manifest, in declaration order."""
        return tuple(self._manifests.values())

    def children_of(self, unit_id: str) -> tuple[str, ...]:
        """Direct attach children of `unit_id` (ROOT_ID allowed), declaration order."""
        if unit_id != ROOT_ID:
            self.get(unit_id)
        return self._children.get(unit_id, ())

    def top_of(self, unit_id: str) -> str:
        manifest = self.get(unit_id)
        while manifest.attach != ROOT_ID:
            manifest = self._manifests[manifest.attach]
        return manifest.id

    def subtree(self, unit_id: str) -> tuple[str, ...]:
        """`unit_id` and all attach descendants, parents before children."""
        self.get(unit_id)
        out: list[str] = []
        stack = [unit_id]
        while stack:
            current = stack.pop()
            out.append(current)
            stack.extend(reversed(self._children.get(current, ())))
        return tuple(out)

    def _children_waves(self, parent_id: str) -> list[tuple[str, ...]]:
        kids = self._children.get(parent_id, ())
        if not kids:
            return []
        by_tier: dict[int, list[str]] = {}
        for kid in kids:
            by_tier.setdefault(self._manifests[kid].stop_tier, []).append(kid)
        waves: list[tuple[str, ...]] = []
        for tier in sorted(by_tier):
            subtrees = [self._unit_waves(kid) for kid in by_tier[tier]]
            depth = max(len(sub) for sub in subtrees)
            padded = [[()] * (depth - len(sub)) + sub for sub in subtrees]
            for level in range(depth):
                merged = tuple(unit for sub in padded for unit in sub[level])
                if merged:
                    waves.append(merged)
        return waves

    def _unit_waves(self, unit_id: str) -> list[tuple[str, ...]]:
        return [*self._children_waves(unit_id), (unit_id,)]

    # meiva:note
    # Attach children finish in earlier waves than their parent; siblings at the same attach
    # level group by ascending `stop_tier` in declaration order.
    # meiva:link services/supervision/meiva_root/docs/closure.meiva.okf.md
    def stop_waves(self, unit_id: str = ROOT_ID) -> tuple[tuple[str, ...], ...]:
        if unit_id == ROOT_ID:
            return tuple(self._children_waves(ROOT_ID))
        self.get(unit_id)
        return tuple(self._unit_waves(unit_id))

    def all_stop_waves(self) -> tuple[tuple[str, ...], ...]:
        return self.stop_waves(ROOT_ID)

    def stop_order(self, unit_id: str) -> tuple[str, ...]:
        self.get(unit_id)
        return tuple(unit for wave in self.stop_waves(unit_id) for unit in wave)

    # meiva:note
    # Every unit, children before parents and tier by tier, preserving declaration order within
    # each wave so tier-0 units stop before later tiers.
    # meiva:link services/agent_runner/agent_host/lifecycle/docs/graceful-exit.meiva.okf.md
    def all_stop_order(self) -> tuple[str, ...]:
        return tuple(unit for wave in self.all_stop_waves() for unit in wave)


def load_manifests(path: Path) -> UnitRegistry:
    """Read a manifest file into a validated registry (fail-fast)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"cannot read manifest file {path}: {exc}") from exc
    try:
        root = object_of(json.loads, text)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{path}: not valid JSON: {exc}") from exc
    if not is_object_dict(root):
        raise ManifestError(f"{path}: top level must be an object with a 'units' list")
    document = root
    unknown = set(document) - _FILE_FIELDS
    if unknown:
        raise ManifestError(f"{path}: unknown top-level field(s) {sorted(unknown)}")
    units_raw = document.get("units")
    if not is_object_list(units_raw):
        raise ManifestError(f"{path}: 'units' must be a list")
    manifests: list[UnitManifest] = []
    for index, item in enumerate(units_raw):
        if not is_object_dict(item):
            raise ManifestError(f"{path}: units[{index}] must be an object")
        manifests.append(UnitManifest.from_mapping(item, origin=f"{path}: units[{index}]"))
    digest = document.get("launch_digest")
    if digest is not None and not isinstance(digest, str):
        raise ManifestError("launch_digest must be a string")
    return UnitRegistry(manifests, launch_digest=digest)
