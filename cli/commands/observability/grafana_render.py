# meiva:note
# `meiva lgtm render` — the meiva-ops dashboard render, from the metric registries.
#
# `base.telemetry.metrics.grafana_dashboard` renders the dashboard from the registered
# `MetricSpec` set and `base.packages.dashboard_supply` collects the plugin side (checkout plugins
# plus enabled installed ones). This command is the operator surface for that render: it diffs the
# render against THIS host's native Grafana provisioning copy (the live `meiva-ops-main.json`) or
# writes the render with `--force`. Converge reuses the same render path, where the render replaces
# the checkout copy as the provisioning source.

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import cast

from base.agents.context.clients import DatabaseFactory
from base.host.atomic_io import write_text_atomic
from cli.commands.observability._lgtm_provisioning import record_rendered_hash
from cli.commands.observability.plans.lgtm.dashboard_plan import RENDERED_DASHBOARD_REL

__all__ = ["cmd_grafana_render"]


# meiva:note
# The native provisioning copy converge renders the dashboard into.
def provisioning_dashboard(home: Path) -> Path:
    return home / "lgtm" / "native" / "config" / RENDERED_DASHBOARD_REL


def _hashes_path(home: Path) -> Path:
    return home / "lgtm" / "native" / "config" / "provisioning-hashes.json"


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# meiva:note
# Panel id and title mapped to the panel JSON for one dashboard file; None when the content is not
# parseable as a dashboard (reported by the caller, not crashed on).
def _panels_by_identity(content: str) -> dict[str, object] | None:
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
    panels = cast("list[dict[str, object]]", data.get("panels", []))
    return {f"{panel.get('id')}:{panel.get('title')}": panel for panel in panels}


# meiva:note
# Print what the render would change about the current file.
def _report_diff(target: Path, current: str, rendered: str) -> None:
    have = _panels_by_identity(current)
    want = _panels_by_identity(rendered)
    if have is None or want is None:
        print(
            f"{target} is not a readable dashboard JSON — the render replaces it wholesale "
            f"({len(want) if want else '?'} panels)",
            file=sys.stderr,
        )
        return
    added = sorted(want.keys() - have.keys())
    removed = sorted(have.keys() - want.keys())
    changed = sorted(key for key in want.keys() & have.keys() if want[key] != have[key])
    print(
        f"render differs: {len(added)} added, {len(removed)} removed, {len(changed)} changed",
        file=sys.stderr,
    )
    for label, keys in (("added", added), ("removed", removed), ("changed", changed)):
        if keys:
            printed = ", ".join(keys[:8]) + (" …" if len(keys) > 8 else "")
            print(f"  {label}: {printed}", file=sys.stderr)


# meiva:note
# Render the meiva-ops dashboard and diff it (default) or write it (`--force`).
#
# The spec set is the checkout's plugins plus, unless `--repo_only`, the enabled installed-plugin
# registry rows: the same dual supplier converge uses. Exit code: 0 when the provisioning copy
# already matches (or was written), 1 when it differs (diff preview) or the render cannot run on
# this host.
def cmd_grafana_render(
    *, home: Path, force: bool, repo_only: bool, database_factory: DatabaseFactory
) -> int:
    target = provisioning_dashboard(home)
    if not target.parent.is_dir():
        print(
            f"no native Grafana provisioning tree at {target.parent} — this host does not "
            "run the LGTM observability stack (`meiva lgtm on` or converge installs it)",
            file=sys.stderr,
        )
        return 1

    from base.packages.dashboard_supply import render_dashboard_json

    rendered, failed = render_dashboard_json(database_factory(), repo_only=repo_only)
    if failed:
        print(
            f"plugin load failures (skipped, panels missing from this render): {', '.join(failed)}",
            file=sys.stderr,
        )
    current = target.read_text(encoding="utf-8") if target.is_file() else None

    if current == rendered:
        record_rendered_hash(_hashes_path(home), RENDERED_DASHBOARD_REL, _digest(rendered))
        print(
            f"{target.name}: in sync ({len(json.loads(rendered)['panels'])} entries, sha256 {_digest(rendered)[:12]})"
        )
        return 0

    if not force:
        if current is None:
            print(f"{target} is absent — the render would create it", file=sys.stderr)
        else:
            _report_diff(target, current, rendered)
        print("run `meiva lgtm render --force` to write the render", file=sys.stderr)
        return 1

    write_text_atomic(
        target, rendered, encoding="utf-8", mode=0o644, sync_file=False, sync_parent=False
    )
    record_rendered_hash(_hashes_path(home), RENDERED_DASHBOARD_REL, _digest(rendered))
    print(
        f"{target}: written ({len(json.loads(rendered)['panels'])} entries, sha256 {_digest(rendered)[:12]})"
    )
    return 0
