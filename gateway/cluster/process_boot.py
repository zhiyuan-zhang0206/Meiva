"""Gateway entry composition; its first-load image is an immutable code fact."""

from __future__ import annotations

from pathlib import Path

from base.native_process.loaded_commit import LoadedCommit

__all__ = ["LOADED_IMAGE"]

# Both executable entries import this module before serving. Ordinary repeated
# imports share this frozen fact; a reload worker is a new process and captures
# its own loaded generation. Mutable admission and resources belong to each root.
LOADED_IMAGE = LoadedCommit.capture(Path(__file__).resolve().parents[2])
