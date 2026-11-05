# meiva:note
# One place that builds a `Settings` over a temporary directory, so a test names only the path it
# is about and every other path lands under `tmp_path` instead of the operator's home.

from __future__ import annotations

from pathlib import Path

from scripts.host_ops.worktree_gc.contracts import Settings


def settings(tmp_path: Path) -> Settings:
    # meiva:note
    # Every path a round touches lives under `tmp_path`; the values a test cares about are the one
    # it passes in, so there is nothing to override here.
    return Settings(
        repo=tmp_path,
        roots=(tmp_path,),
        probe=None,
        probe_ledger=tmp_path / "probe-ledger.jsonl",
        hold=tmp_path / "hold.json",
        manifests=tmp_path / "manifests.json",
        ledger=tmp_path / "ledger.jsonl",
        never=tmp_path / "never.txt",
        blind_state=tmp_path / "blind-state.json",
        home_env=tmp_path / ".env",
        owner=1818,
        gateway="",
    )
