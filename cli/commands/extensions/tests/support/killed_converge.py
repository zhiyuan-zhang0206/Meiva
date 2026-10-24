# meiva:note
# The states a converge killed part-way leaves behind, built from the bridge's own steps.
#
# A converge that dies leaves exactly what its last completed step wrote: a ledger line and a
# directory move. The functions here run the bridge's public steps up to a point and stop, so a
# test starts from a real ledger and real directories and asks what the NEXT converge does with
# them. `stage` is the first step (it prepares, verifies and publishes the staged copy and
# records each phase); `claim_target`, `record_claim` and `land_stage` are the activation steps in
# the order the bridge takes them; `forget_publication` puts back the one ledger line a kill
# between the stage rename and its record would not have written.

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from base.paths.private_dir import ensure_private_dir
from cli.commands.extensions import external_skills as bridge
from cli.commands.extensions.external_skill_host import filesystem as bridge_fs
from cli.commands.extensions.external_skill_host.cleanup import quarantine_path, transaction_path
from cli.commands.extensions.external_skill_host.ledger import FORMAT, load_ledger, write_ledger

__all__ = [
    "Site",
    "claim_cleanup_file",
    "claim_cleanup_root",
    "claim_target",
    "commit",
    "forget_publication",
    "land_stage",
    "record_claim",
    "stage",
]


# meiva:note
# One client's copy of one skill: the client's `skills` directory, the ledger file the bridge
# keeps for it under the meiva home, the ledger's client key and the skill's source tree.
@dataclass(frozen=True)
class Site:
    skills_root: Path
    ledger_path: Path
    client_key: str
    skill: str
    source: Path

    @property
    def target(self) -> Path:
        return self.skills_root / self.skill


def _ledger(site: Site) -> dict[str, Any]:
    ledger = load_ledger(site.ledger_path, site.client_key)
    if ledger is not None:
        return ledger
    return {
        "client": site.client_key,
        "format": FORMAT,
        "garbage": [],
        "installation_id": uuid.uuid4().hex,
        "installed": None,
        "retained": [],
        "transaction": None,
    }


def _path(site: Site, kind: str, ledger: dict[str, Any]) -> Path:
    generation_id = ledger["transaction"]["generation_id"]
    return transaction_path(site.skills_root, kind, generation_id, skill_name=site.skill)


# meiva:note
# Snapshot the source and take the bridge's first step: the staged copy is prepared, verified,
# published beside the target and recorded as `published`. Returns the ledger with its open
# transaction.
def stage(site: Site) -> dict[str, Any]:
    ensure_private_dir(site.ledger_path.parent)
    site.skills_root.mkdir(exist_ok=True)
    ledger = _ledger(site)
    snapshot = bridge_fs.source_snapshot(site.source)
    manifest = snapshot.manifest()
    bridge.stage_copy(
        snapshot,
        manifest,
        site.skills_root,
        site.ledger_path,
        ledger,
        bridge_fs.manifest_digest(manifest),
        skill_name=site.skill,
    )
    return ledger


# meiva:note
# The ledger as a process killed after it renamed the prepared copy into place, and before it
# wrote that down, leaves it: the stage exists and the record still says `publishing`.
def forget_publication(site: Site, ledger: dict[str, Any]) -> None:
    ledger["transaction"]["stage_state"] = "publishing"
    write_ledger(site.ledger_path, ledger)


# meiva:note
# The activation's first step, up to the move: the claim is written ahead as `claiming`, then the
# managed target is renamed aside to its transaction-owned `previous` path. A process killed here
# leaves the move done and the record at `claiming`.
def claim_target(site: Site, ledger: dict[str, Any]) -> Path:
    ledger["transaction"]["claim_state"] = "claiming"
    write_ledger(site.ledger_path, ledger)
    previous = _path(site, "previous", ledger)
    bridge_fs.rename_no_replace(site.target, previous)
    return previous


# meiva:note
# The record that follows the claim's move.
def record_claim(site: Site, ledger: dict[str, Any]) -> None:
    ledger["transaction"]["claim_state"] = "claimed"
    write_ledger(site.ledger_path, ledger)


# meiva:note
# The activation's last move: the staged copy is renamed onto the target. The ledger still holds
# the open transaction, because committing it is the step after.
def land_stage(site: Site, ledger: dict[str, Any]) -> None:
    bridge_fs.rename_no_replace(_path(site, "stage", ledger), site.target)


# meiva:note
# Commit the activation the way the bridge does once the staged copy is live: the new copy
# becomes the installed one, the transaction closes and the superseded copy is queued as garbage.
# Returns that garbage item, whose location is `source` until a cleanup claims it.
def commit(site: Site, ledger: dict[str, Any], previous: Path) -> dict[str, Any]:
    bridge.commit_activation(site.ledger_path, ledger, ledger["transaction"], previous)
    return ledger["garbage"][-1]


# meiva:note
# Cleanup's private claim of a garbage item's root, done: the superseded copy is renamed from the
# skills directory into the ledger directory and the item is recorded as `quarantine`. Returns the
# quarantine path. A process killed between the rename and the record leaves `claiming`
# (`record=False`).
def claim_cleanup_root(
    site: Site, ledger: dict[str, Any], item: dict[str, Any], *, record: bool = True
) -> Path:
    source = transaction_path(
        site.skills_root, item["kind"], item["path_generation_id"], skill_name=site.skill
    )
    quarantine = quarantine_path(site.ledger_path, item["kind"], item["path_generation_id"])
    item["location"] = "claiming"
    write_ledger(site.ledger_path, ledger)
    bridge_fs.rename_no_replace(source, quarantine)
    if record:
        item["location"] = "quarantine"
        write_ledger(site.ledger_path, ledger)
    return quarantine


# meiva:note
# Cleanup's claim of one file inside a quarantined root: the claim is written ahead as `claiming`,
# the file is renamed to its claim name and the claim is recorded as `quarantine`. A process
# killed between the rename and the record leaves the file moved and the claim `claiming`
# (`record=False`).
def claim_cleanup_file(
    site: Site,
    ledger: dict[str, Any],
    quarantine: Path,
    claim: dict[str, str],
    *,
    record: bool = True,
) -> Path:
    claim["state"] = "claiming"
    write_ledger(site.ledger_path, ledger)
    claimed = quarantine / claim["quarantine"]
    bridge_fs.rename_no_replace(quarantine / claim["path"], claimed)
    if record:
        claim["state"] = "quarantine"
        write_ledger(site.ledger_path, ledger)
    return claimed
