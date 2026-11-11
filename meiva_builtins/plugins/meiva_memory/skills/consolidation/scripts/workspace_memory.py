from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from math import ceil
from pathlib import Path

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_of


def summary(text: str) -> dict[str, str]:
    headings = [
        line.lstrip("#").strip().casefold() for line in text.splitlines() if line.startswith("#")
    ]
    tokens = sorted(
        set(re.findall(r"[\w]+", text.casefold()))
        - {"the", "a", "an", "and", "to", "of", "in", "is", "for"}
    )
    return {
        "title": headings[0] if headings else "",
        "tokens": " ".join(tokens),
        "headings": "\n".join(headings),
    }


def collect_candidates(home: Path, machine: str) -> list[dict[str, str]]:
    if not machine or ":" in machine or "/" in machine:
        raise ValueError("machine must be a non-empty name without ':' or '/'")
    root = home / "workspaces"
    result: list[dict[str, str]] = []
    for path in sorted({*root.glob("*/memory/*.md"), *root.glob("*/memory/inherit/*.md")}):
        if path.is_symlink():
            continue
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8")
            modified = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.UTC).date()
        except (OSError, UnicodeDecodeError):
            continue
        if not text.strip():
            continue
        rel = path.relative_to(root).as_posix()
        result.append(
            {
                "machine": machine,
                "agent_id": rel.split("/")[0],
                "path": rel,
                "modified": modified.isoformat(),
                "sha256": hashlib.sha256(raw).hexdigest(),
                **summary(text),
            }
        )
    return result


def _valid_path(raw: str) -> Path:
    path = Path(raw)
    if (
        path.is_absolute()
        or ".." in path.parts
        or len(path.parts) < 3
        or path.parts[1] != "memory"
        or path.suffix != ".md"
    ):
        raise ValueError(f"Invalid workspace note path: {raw}")
    return path


def candidate_key(candidate: dict[str, str]) -> str:
    return f"{candidate['machine']}:{candidate['path']}"


def _candidate(value: object) -> dict[str, str]:
    if not is_object_dict(value) or not all(isinstance(v, str) for v in value.values()):
        raise ValueError("Remote candidates must contain string-valued objects")
    row = {k: v for k, v in value.items() if isinstance(v, str)}
    for key in ("machine", "agent_id", "path", "modified", "sha256", "title", "tokens", "headings"):
        if key not in row:
            raise ValueError(f"Remote candidate is missing {key}")
    path = _valid_path(row["path"])
    if (
        path.parts[0] != row["agent_id"]
        or not row["machine"]
        or ":" in row["machine"]
        or "/" in row["machine"]
    ):
        raise ValueError("Remote candidate identity does not match its path")
    _ = dt.date.fromisoformat(row["modified"])
    if re.fullmatch("[0-9a-f]{64}", row["sha256"]) is None:
        raise ValueError("Invalid candidate sha256")
    return row


def merge_candidates(
    local: Sequence[dict[str, str]], remote_paths: Sequence[Path]
) -> list[dict[str, str]]:
    rows = {candidate_key(row): row for row in local}
    for path in remote_paths:
        payload = object_of(json.loads, path.read_text(encoding="utf-8"))
        if not is_object_list(payload):
            raise ValueError(f"{path}: expected a JSON list")
        for item in payload:
            row = _candidate(item)
            key = candidate_key(row)
            if key in rows and rows[key] != row:
                raise ValueError(f"Conflicting candidate exports for {key}")
            rows[key] = row
    return [rows[key] for key in sorted(rows)]


def _entries(ledger: Path) -> dict[str, object]:
    if not ledger.exists():
        return {}
    payload = object_of(json.loads, ledger.read_text(encoding="utf-8"))
    if not is_object_dict(payload) or not is_object_dict(entries := payload.get("entries")):
        raise ValueError(f"{ledger}: expected an entries object")
    return entries


def filter_candidates(
    candidates: Sequence[dict[str, str]],
    ledger: Path,
    since_cutoff: dt.date,
    include_unreviewed: bool,
) -> tuple[list[dict[str, str]], int]:
    entries = _entries(ledger)
    use_hash = ledger.exists() or include_unreviewed
    result: list[dict[str, str]] = []
    skipped = 0
    for row in candidates:
        entry = entries.get(candidate_key(row), entries.get(row["path"]))
        if use_hash and is_object_dict(entry) and entry.get("sha256") == row["sha256"]:
            skipped += 1
            continue
        if use_hash:
            result.append(
                {**row, "status": "modified_since_review" if entry is not None else "unreviewed"}
            )
        elif dt.date.fromisoformat(row["modified"]) >= since_cutoff:
            result.append(dict(row))
    return result, skipped


def _similarity(left: dict[str, str], right: dict[str, str]) -> float:
    a, b = set(left.get("tokens", "").split()), set(right.get("tokens", "").split())
    lexical = len(a & b) / len(a | b) if a | b else 0.0
    ah = {h for h in left.get("headings", "").splitlines() if len(h.split()) >= 2}
    bh = set(right.get("headings", "").splitlines())
    return max(lexical, 0.8 if ah & bh else 0.0)


@dataclass(frozen=True)
class _Parsed:
    tokens: frozenset[str]
    multi_headings: frozenset[str]
    headings: frozenset[str]

    @classmethod
    def from_row(cls, row: dict[str, str]) -> _Parsed:
        headings = frozenset(row.get("headings", "").splitlines())
        return cls(
            frozenset(row.get("tokens", "").split()),
            frozenset(h for h in headings if len(h.split()) >= 2),
            headings,
        )

    def similarity(self, other: _Parsed) -> float:
        common = len(self.tokens & other.tokens)
        union = len(self.tokens) + len(other.tokens) - common
        return max(
            common / union if union else 0.0, 0.8 if self.multi_headings & other.headings else 0.0
        )


class _SimilarityIndex:
    def __init__(self, rows: Sequence[_Parsed], *, threshold: bool) -> None:
        self.rows = rows
        self.threshold = threshold
        self.frequency = Counter(token for row in rows for token in row.tokens)
        self.tokens: dict[str, set[int]] = defaultdict(set)
        self.headings: dict[str, set[int]] = defaultdict(set)
        for i, row in enumerate(rows):
            for token in self._prefix(row):
                self.tokens[token].add(i)
            for heading in row.headings:
                self.headings[heading].add(i)

    def _prefix(self, row: _Parsed) -> Sequence[str]:
        if not self.threshold:
            return list(row.tokens)
        # meiva:note
        # Jaccard >= t requires a shared token in these globally ordered prefixes:
        # disjoint prefixes leave fewer than ceil(t * size) possible matches.
        ordered = sorted(row.tokens, key=lambda token: (self.frequency[token], token))
        return ordered[: len(ordered) - ceil(0.45 * len(ordered)) + 1]

    def maximum(self, row: _Parsed) -> float:
        return max((row.similarity(self.rows[i]) for i in self.matches(row)), default=0.0)

    def matches(self, row: _Parsed) -> set[int]:
        lexical: set[int] = set()
        for token in self._prefix(row):
            lexical.update(self.tokens.get(token, ()))
        if self.threshold:
            lexical = {
                i
                for i in lexical
                if min(len(row.tokens), len(self.rows[i].tokens))
                >= 0.45 * max(len(row.tokens), len(self.rows[i].tokens))
            }
        # meiva:note
        # Heading matches bypass token-size pruning, including empty token sets.
        for heading in row.multi_headings:
            lexical.update(self.headings.get(heading, ()))
        return lexical


def _demands(path: Path | None) -> list[dict[str, str]]:
    if path is None:
        return []
    rows: list[dict[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = object_of(json.loads, line)
        if not is_object_dict(value) or not isinstance(text := value.get("text"), str):
            raise ValueError("Demand JSONL requires objects with text and agent_id or kind")
        kind, agent = value.get("kind"), value.get("agent_id")
        if not isinstance(agent, (str, int)) and kind not in ("user-preference", "platform-rule"):
            raise ValueError("Demand needs agent_id or a user-preference/platform-rule kind")
        rows.append(
            {
                **summary(text),
                "agent_id": str(agent) if agent is not None else "",
                "kind": str(kind) if kind is not None else "",
            }
        )
    return rows


def scope_traps(
    candidates: Sequence[dict[str, str]], pool_texts: Sequence[str], demand_path: Path | None
) -> list[dict[str, object]]:
    parsed = [_Parsed.from_row(row) for row in candidates]
    pool = [_Parsed.from_row(summary(text)) for text in pool_texts]
    demands = _demands(demand_path)
    demand_rows = [_Parsed.from_row(row) for row in demands]
    peers_index = _SimilarityIndex(parsed, threshold=True)
    pool_index = _SimilarityIndex(pool, threshold=False)
    demand_index = _SimilarityIndex(demand_rows, threshold=True)
    result: list[dict[str, object]] = []
    for row, record in zip(candidates, parsed, strict=True):
        coverage = pool_index.maximum(record)
        if coverage >= 0.45:
            continue
        peers = sorted(
            candidate_key(candidates[i])
            for i in peers_index.matches(record)
            if candidates[i]["agent_id"] != row["agent_id"] and record.similarity(parsed[i]) >= 0.45
        )
        demand = any(
            record.similarity(demand_rows[i]) >= 0.45
            and (
                demands[i]["agent_id"] != row["agent_id"]
                or demands[i]["kind"] in ("user-preference", "platform-rule")
            )
            for i in demand_index.matches(record)
        )
        if peers or demand:
            result.append(
                {
                    **row,
                    "priority": "P0",
                    "overlapping_notes": peers,
                    "demand_match": demand,
                    "max_pool_similarity": round(coverage, 4),
                }
            )
    return result


def _private_target(home: Path, raw: str) -> Path:
    rel = _valid_path(raw)
    root = (home / "workspaces").resolve()
    target = (root / rel).resolve()
    if not target.is_relative_to(root / rel.parts[0] / "memory"):
        raise ValueError("Private note escapes workspaces")
    return target


def _rewrite_index(index: Path, note: Path, destination: str) -> bool:
    if index.is_symlink():
        raise ValueError("Private index must not be a symlink")
    if not index.exists():
        return False
    text = index.read_text(encoding="utf-8")

    def replace(match: re.Match[str]) -> str:
        raw = match.group(1)
        if (index.parent / raw).resolve() == note:
            return f"]({destination})"
        return match.group(0)

    updated = re.sub(r"\]\(([^)]+)\)", replace, text)
    if updated != text:
        _ = index.write_text(updated, encoding="utf-8")
    return updated != text


def _prune(note: Path, promoted_to: str) -> None:
    destination = f"~/.meiva/vault/memory/{promoted_to}"
    _ = _rewrite_index(
        note.parent.parent / "MEMORY.md"
        if note.parent.name == "inherit"
        else note.parent / "MEMORY.md",
        note,
        destination,
    )
    memory = note.parent.parent if note.parent.name == "inherit" else note.parent
    referenced = any(
        note.name in other.read_text(encoding="utf-8")
        for other in memory.rglob("*.md")
        if other != note and other.name != "MEMORY.md"
    )
    if referenced:
        _ = note.write_text(f"[Shared memory]({destination})\n", encoding="utf-8")
    else:
        note.unlink()


def _validate_promotion(status: str, promoted_to: str | None, pool: Path) -> None:
    if status == "promoted":
        if not promoted_to or Path(promoted_to).is_absolute() or ".." in Path(promoted_to).parts:
            raise ValueError("Promoted review requires a safe pool-relative target")
        target = (pool / promoted_to).resolve()
        if not target.is_relative_to(pool.resolve()) or not target.is_file():
            raise ValueError("Promoted target must exist in the shared pool")
    elif promoted_to is not None:
        raise ValueError("local_only review cannot name promoted_to")


def record_review(
    home: Path,
    ledger: Path,
    key: str,
    status: str,
    promoted_to: str | None,
    today: dt.date,
    prune_private: bool,
    machine: str,
    pool: Path,
) -> None:
    if status not in ("promoted", "local_only"):
        raise ValueError("Review status must be promoted or local_only")
    host, separator, raw = key.partition(":")
    if separator and host != machine:
        raise ValueError("Record reviews on the machine owning the private note")
    raw = raw if separator else key
    note = _private_target(home, raw)
    if prune_private and (status != "promoted" or note.name in ("MEMORY.md", "INHERIT.md")):
        raise ValueError("Pruning requires a promoted detail note")
    _validate_promotion(status, promoted_to, pool)
    sha = hashlib.sha256(note.read_bytes()).hexdigest()
    entries = _entries(ledger)
    if prune_private and promoted_to is not None:
        _prune(note, promoted_to)
        if note.exists():
            sha = hashlib.sha256(note.read_bytes()).hexdigest()
    entries[f"{machine}:{raw}"] = {
        "sha256": sha,
        "status": "pruned" if prune_private else status,
        "promoted_to": promoted_to,
        "reviewed_at": today.isoformat(),
    }
    ledger.parent.mkdir(parents=True, exist_ok=True)
    temporary = ledger.with_suffix(".tmp")
    _ = temporary.write_text(
        json.dumps({"entries": entries}, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _ = temporary.replace(ledger)
