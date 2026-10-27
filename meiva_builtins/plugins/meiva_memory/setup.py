"""Host-side scaffolding for the explicit `meiva memory init` command.

Kept apart from `plugin.py` on purpose: that module imports the agent runtime
(hooks, graph, the SDK namespace), none of which exists in the `meiva` CLI
process. This one depends on `base` alone, so `meiva memory init` can load it
without dragging an agent into a CLI command.

What it owns is the template laid down inside the pool checkout: the index every
agent reads, and the commit hook enforcing the character caps. (The .gitignore is
not in the template — `memory_repo.init` writes it as the checkout's first
commit, since a repo needs a file to commit; one authority, not two.)

Before this step existed the template was a directory of files nothing copied,
and a pool came up holding only that .gitignore: no MEMORY.md, so the index
injection silently found nothing to inject, and no armed hook, so the caps never
fired. Prose in a skill told an agent to copy them by hand; explicit provisioning
of an invariant is not a thing to leave to prose.

Idempotent, and non-destructive: an existing file is never overwritten. The
template seeds a pool, it does not reset one — an agent's curated index must
survive every explicit initialization.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from base.cluster.machine import MachineIdentityReader
from base.log import logger
from base.packages.plugins.loading.scaffold import ScaffoldFacts

_TEMPLATE_DIR = Path(__file__).parent / "template"

# git refuses to track an empty directory, so the template carries a .gitkeep to
# materialize `machines/`; the marker itself is not interesting to copy around.
_GITKEEP = ".gitkeep"


def _copy_template_into(pool: Path) -> list[str]:
    """Copy every template file that is missing from `pool`. Returns the
    pool-relative paths actually written, for the converge log."""
    written: list[str] = []
    for src in sorted(_TEMPLATE_DIR.rglob("*")):
        if src.is_dir():
            continue
        rel = src.relative_to(_TEMPLATE_DIR)
        dst = pool / rel
        if dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        # copy2 preserves the executable bit, which the commit hook needs.
        written.append(str(rel))
    return [w for w in written if not w.endswith(_GITKEEP)]


def _arm_hooks(pool: Path) -> bool:
    """Point the checkout's `core.hooksPath` at the template's `.githooks`.
    Returns True when this call changed it. Without this the pre-commit cap
    guard sits on disk unarmed — which is how it sat for the pool's whole life
    before this step."""
    current = subprocess.run(  # noqa: S603
        ["git", "-C", str(pool), "config", "--get", "core.hooksPath"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if current == ".githooks":
        return False
    subprocess.run(  # noqa: S603
        ["git", "-C", str(pool), "config", "core.hooksPath", ".githooks"],
        check=True,
    )
    return True


def _initialize_authoring(
    *, machine: MachineIdentityReader, facts: ScaffoldFacts, target: Path
) -> None:
    # meiva:note
    # The authoring stage's provisioning inputs are the `facts` the command read; nothing here
    # reads the process settings.
    # meiva:link meiva_builtins/plugins/meiva_memory/docs/memory-pool-scaffold.meiva.okf.md
    from base.deploy.git.memory_repo import (
        MemoryPoolBootstrapFailed,
        MemoryRemoteMissing,
        bootstrap_from_gateway,
        branch_name,
        init,
        init_local_repo,
        is_initialized,
        memory_remote,
        reconcile_initialized,
    )

    branch = branch_name(machine=machine)
    keep_local = facts.memory_keep_local
    if is_initialized(root=target):
        reconcile_initialized(
            root=target,
            branch=branch,
            keep_local=keep_local,
            allow_main_switch="gateway" in machine.role() and "agent-runner" not in machine.role(),
        )
        return
    if keep_local:
        init_local_repo(branch, target)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        remote = memory_remote(facts.memory_remote)
    except MemoryRemoteMissing:
        if "agent-runner" in machine.role() and "gateway" not in machine.role():
            try:
                bootstrap_from_gateway(target=target, branch=branch, access=facts.gateway)
            except MemoryPoolBootstrapFailed as error:
                raise MemoryRemoteMissing(
                    "memory remote not configured and the gateway pool snapshot "
                    f"fetch failed ({error}). Set MEIVA_MEMORY_REMOTE for the GitHub sync "
                    "path, fix the gateway URL / cluster secret, or set "
                    "MEIVA_MEMORY_KEEP_LOCAL=true "
                    "to run an explicitly local-only pool."
                ) from error
            return
        init_local_repo(branch, target)
        return
    init(root=target, branch=branch, remote=remote)


def _ensure_memory_repo(
    *, machine: MachineIdentityReader, facts: ScaffoldFacts, target: Path
) -> None:
    # meiva:note
    # Keep the outer probe's logging and error boundary separate from init's probe.
    # meiva:link meiva_builtins/plugins/meiva_memory/docs/memory-pool-scaffold.meiva.okf.md
    from base.deploy.git.memory_repo import branch_name, is_initialized

    if is_initialized(root=target):
        _initialize_authoring(machine=machine, facts=facts, target=target)
        return
    branch = branch_name(machine=machine)
    print(f"    first-time setup: bring up memory pool -> {target} on branch {branch!r}")
    try:
        _initialize_authoring(machine=machine, facts=facts, target=target)
    except subprocess.CalledProcessError as error:
        stderr = error.stderr.decode() if isinstance(error.stderr, bytes) else (error.stderr or "")
        raise RuntimeError(_classify_git_init_error(stderr, error.returncode)) from error
    print("    memory pool ready")


def _initialize_gateway(*, facts: ScaffoldFacts, target: Path) -> None:
    # meiva:note
    # The consolidated checkout's provisioning inputs are the `facts` the command read.
    # meiva:link meiva_builtins/plugins/meiva_memory/docs/memory-pool-scaffold.meiva.okf.md
    from base.deploy.git.memory_repo import (
        MemoryRemoteMissing,
        init,
        init_local_repo,
        is_initialized,
        memory_remote,
        reconcile_initialized,
    )

    keep_local = facts.memory_keep_local
    if is_initialized(root=target):
        reconcile_initialized(
            root=target, branch="main", keep_local=keep_local, allow_main_switch=True
        )
        return
    if keep_local:
        init_local_repo("main", target)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        remote = memory_remote(facts.memory_remote)
    except MemoryRemoteMissing:
        init_local_repo("main", target)
        return
    init(root=target, branch="main", remote=remote)


def _ensure_gateway_memory_repo(
    *, machine: MachineIdentityReader, facts: ScaffoldFacts, target: Path
) -> None:
    # meiva:note
    # Provision the captured consolidated target only for a gateway-capable unit.
    # meiva:link meiva_builtins/plugins/meiva_memory/docs/memory-pool-scaffold.meiva.okf.md
    from base.deploy.git.memory_repo import is_initialized

    if "gateway" not in machine.role():
        return
    if is_initialized(root=target):
        _initialize_gateway(facts=facts, target=target)
        return
    print(f"    first-time setup: clone gateway memory pool -> {target} on branch 'main'")
    try:
        _initialize_gateway(facts=facts, target=target)
    except subprocess.CalledProcessError as error:
        stderr = error.stderr.decode() if isinstance(error.stderr, bytes) else (error.stderr or "")
        raise RuntimeError(_classify_git_init_error(stderr, error.returncode)) from error
    print("    gateway memory pool ready")


def _classify_git_init_error(stderr: str, returncode: int) -> str:
    """Map a git-clone/push stderr blob to a single-line, actionable hint.

    Three known failure shapes get a targeted next-command; anything else falls
    through to the raw stderr so the agent still has the original text to act on.
    """
    blob = stderr.lower()
    header = f"git command failed (returncode={returncode}):\n{stderr}\n"
    if "permission denied (publickey)" in blob or "could not read from remote repository" in blob:
        return (
            f"{header}→ SSH auth to git remote failed. Verify with `ssh -T git@github.com`; "
            "if it prints `Permission denied`, add this host's `~/.ssh/id_*.pub` to your GitHub keys "
            "(Settings > SSH and GPG keys)."
        )
    if "repository not found" in blob or "does not appear to be a git repository" in blob:
        return (
            f"{header}→ Remote repo does not exist (or this host's SSH key lacks access). "
            "Create a private repo at the URL set in `MEIVA_MEMORY_REMOTE` (e.g. `gh repo create <user>/MeivaMemory --private`), "
            "or fix the URL in `~/.meiva/.env`."
        )
    if "couldn't find remote ref" in blob or "not found in upstream" in blob:
        return (
            f"{header}→ Remote branch does not exist yet. This is normal on first-ever clone — "
            "the local `init()` will push an empty branch on the next retry. If retry still fails, "
            "check that `MEIVA_MACHINE_NAME` matches the expected branch suffix."
        )
    return f"{header}(unrecognized failure shape — paste the stderr above into the runbook 'Secondary host bring-up' troubleshooting section)"


def scaffold(*, machine: MachineIdentityReader, facts: ScaffoldFacts) -> None:
    """Bring this cluster's memory pool up during explicit initialization.

    Three things, in order: the agent-runner's authoring checkout, the gateway's
    consolidated one (gateway-capable units only), and the template laid down
    inside the authoring checkout.

    It is here because the pool is this plugin's, end to end: disable the plugin
    and no checkout is created, no template is laid down, and no memory note
    reaches an agent's context.
    """
    from base.paths import gateway_memory_dir, memory_dir

    pool = memory_dir(home=facts.home)
    consolidated = gateway_memory_dir(roles=machine.role(), home=facts.home)
    _ensure_memory_repo(machine=machine, facts=facts, target=pool)
    _ensure_gateway_memory_repo(machine=machine, facts=facts, target=consolidated)
    if not (pool / ".git").is_dir():
        # The bring-up above declined and said why (no remote, wrong branch).
        logger.debug("[memory-scaffold] {} is not a git checkout, skipping", pool)
        return

    written = _copy_template_into(pool)
    armed = _arm_hooks(pool)
    if written:
        print(f"    seeded: {', '.join(written)}")
    if armed:
        print("    armed commit hook (core.hooksPath=.githooks)")
    logger.info("[memory-scaffold] pool={} seeded={} hook_armed={}", pool, len(written), armed)
