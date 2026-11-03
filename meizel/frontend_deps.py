from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from meizel.graph.model import MeizelError

FRONTEND_DIR = ("ui", "web")
SOURCE_LOCK = "pnpm-lock.yaml"
NODE_MODULES = "node_modules"
PNPM_METADATA = (NODE_MODULES, ".modules.yaml")
TREE_LOCK = (NODE_MODULES, ".pnpm", "lock.yaml")


class FrontendDeps(StrEnum):
    READY = "ready"
    MISSING = "missing"
    NPM_ERA = "npm-era"
    LOCK_CHANGED = "lock-changed"


class FrontendDepsError(MeizelError):
    pass


@dataclass(frozen=True)
class FrontendObservation:
    node_modules: bool
    pnpm_metadata: bool
    installed_lock: str | None
    source_lock: str | None


# meiva:note
# One verdict per install shape, in the order the probes can tell them apart: no tree at all, a tree
# with no pnpm metadata (installed before #8085), a tree pnpm built for another lockfile.
# meiva:link meizel/docs/frontend-deps.meiva.okf.md
def classify(observation: FrontendObservation) -> FrontendDeps:
    if not observation.node_modules:
        return FrontendDeps.MISSING
    if not observation.pnpm_metadata:
        return FrontendDeps.NPM_ERA
    if observation.installed_lock is None or observation.installed_lock != observation.source_lock:
        return FrontendDeps.LOCK_CHANGED
    return FrontendDeps.READY


# meiva:note
# A refusal names the state it found and the one command that ends it; a ready tree has no refusal,
# and asking for one is a programming error rather than a message.
# meiva:link meizel/docs/frontend-deps.meiva.okf.md
def refusal(state: FrontendDeps) -> str:
    frontend = "/".join(FRONTEND_DIR)
    tree = f"{frontend}/{NODE_MODULES}"
    reinstall = f"(cd {frontend} && pnpm install --frozen-lockfile)"
    if state is FrontendDeps.MISSING:
        return (
            f"frontend dependencies are not installed ({tree} is missing) — run: "
            "bash scripts/setup-worktree.sh"
        )
    if state is FrontendDeps.NPM_ERA:
        return (
            f"{tree} is a stale npm-era install (the frontend moved to pnpm in #8085), "
            f"so its graph is not the one {SOURCE_LOCK} pins — re-run: {reinstall}"
        )
    if state is FrontendDeps.LOCK_CHANGED:
        return f"{tree} was installed for a different lockfile than {SOURCE_LOCK} — re-run: {reinstall}"
    raise ValueError(f"{state} is not a refusal")
