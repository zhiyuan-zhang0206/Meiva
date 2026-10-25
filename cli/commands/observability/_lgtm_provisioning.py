from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TypeGuard, assert_never

import httpx

from base import telemetry
from base.agents.context.clients import DatabaseFactory
from base.host.atomic_io import write_text_atomic
from base.log import logger
from base.telemetry import EventPipeline
from cli.commands.converge.rendered_file import write_rendered_guarded
from cli.commands.observability.plans.lgtm.dashboard_checks import (
    GrafanaReply,
    file_problems,
    grafana_problems,
    sidecar_problems,
)
from cli.commands.observability.plans.lgtm.dashboard_plan import (
    DASHBOARD_UID,
    RENDERED_DASHBOARD_REL,
    DashboardFacts,
    DashboardOutcome,
    FailedDashboardRender,
    InstallReference,
    KeepInstalled,
    KeepRendered,
    RefuseDashboard,
    RenderedDashboard,
    WriteRendered,
    ghost_hash_keys,
    hash_record_update,
    orphan_removable,
    plan_dashboard_fallback,
    plan_rendered_dashboard,
)


# meiva:note
# Render the `meiva-ops` dashboard from the metric registries and the enabled plugins, or report
# why it could not.
#
# The plugin machinery is imported here, not at module level: the converge step table imports
# this module on every CLI start, and the render is needed only when a station converges. A
# render that raises is an outcome, not an error: converge installs a fallback instead.
def render_dashboard(database_factory: DatabaseFactory) -> DashboardOutcome:
    from base.packages.dashboard_supply import render_dashboard_json

    try:
        rendered, failed = render_dashboard_json(database_factory())
    except Exception as exc:
        return FailedDashboardRender(str(exc))
    return RenderedDashboard(rendered, tuple(failed))


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# meiva:link cli/commands/observability/docs/lgtm/dashboard-provisioning.meiva.okf.md
def _provision_dashboard(
    dest: Path,
    hashes_path: Path,
    key: str,
    reference: Path,
    *,
    outcome: DashboardOutcome,
    producer: Callable[[], EventPipeline],
) -> None:
    match outcome:
        case FailedDashboardRender():
            print(
                f"  ! lgtm native: meiva-ops dashboard render failed ({outcome.error})",
                file=sys.stderr,
            )
            _install_dashboard_fallback(
                dest, hashes_path, key, reference, error=outcome.error[:300], producer=producer
            )
            return
        case RenderedDashboard():
            pass
        case _:
            assert_never(outcome)
    if outcome.failed_plugins:
        print(
            f"  ! lgtm native: plugin load failures (skipped, panels missing from this render): "
            f"{', '.join(outcome.failed_plugins)}",
            file=sys.stderr,
        )
    digest = _digest(outcome.content)
    current = dest.read_text(encoding="utf-8") if dest.exists() else None
    rendered_plan = plan_rendered_dashboard(
        current=current, rendered=outcome.content, digest=digest
    )
    match rendered_plan:
        case KeepRendered():
            record_rendered_hash(hashes_path, key, digest)
        case WriteRendered():
            dest.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomic(dest, outcome.content, encoding="utf-8", suffix="")
            record_rendered_hash(hashes_path, key, digest)
            print(f"  lgtm native: meiva-ops dashboard re-rendered (sha256 {digest[:12]})")
        case _:
            assert_never(rendered_plan)


# meiva:link cli/commands/observability/docs/lgtm/dashboard-provisioning.meiva.okf.md
def _install_dashboard_fallback(
    dest: Path,
    hashes_path: Path,
    key: str,
    reference: Path,
    *,
    error: str,
    producer: Callable[[], EventPipeline],
) -> None:
    reference_text = reference.read_text(encoding="utf-8") if reference.is_file() else ""
    plan = plan_dashboard_fallback(
        installed=dashboard_facts(dest),
        reference_text=reference_text,
        reference=dashboard_facts(reference)
        if reference_text
        else DashboardFacts(uid="", has_panels=False),
        digest=_digest(reference_text),
    )
    match plan:
        case KeepInstalled():
            outcome = "kept"
            print(f"  ! lgtm native: keeping the installed dashboard at {dest}", file=sys.stderr)
        case InstallReference(digest=digest):
            outcome = "reference"
            dest.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomic(dest, reference_text, encoding="utf-8", suffix="")
            record_rendered_hash(hashes_path, key, digest, source="reference")
            print(
                f"  ! lgtm native: no usable installed dashboard; "
                f"installed the checkout reference {reference}",
                file=sys.stderr,
            )
        case RefuseDashboard():
            telemetry.emit(
                "telemetry",
                "lgtm_dashboard_render_failed",
                producer=producer,
                level="error",
                source="converge",
                attributes={"error": error, "outcome": "failed"},
            )
            raise RuntimeError(
                "meiva-ops dashboard render failed, the installed file is not this dashboard, and "
                f"the checkout reference {reference} is missing or unusable: refusing to converge "
                "with no dashboards"
            )
        case _:
            assert_never(plan)
    telemetry.emit(
        "telemetry",
        "lgtm_dashboard_render_failed",
        producer=producer,
        level="warning",
        source="converge",
        attributes={"error": error, "outcome": outcome},
    )


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


# meiva:note
# The parsed JSON object at `path`, or an empty mapping when it is missing, unreadable or not an
# object. The reason is logged: a dashboard that is not provisioned this pass must not vanish
# silently.
def dashboard_document(path: Path) -> dict[str, object]:
    try:
        document: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(
            "dashboard {path} could not be read ({err}); it is not provisioned this pass",
            path=str(path),
            err=repr(exc),
            event="lgtm_dashboard",
        )
        return {}
    if not _is_object_dict(document):
        logger.warning(
            "dashboard {path} is not a JSON object; it is not provisioned this pass",
            path=str(path),
            event="lgtm_dashboard",
        )
        return {}
    return document


# meiva:note
# The uid and panel presence of the dashboard file at `path`.
def dashboard_facts(path: Path) -> DashboardFacts:
    document = dashboard_document(path)
    uid = document.get("uid")
    panels = document.get("panels")
    return DashboardFacts(
        uid=uid if isinstance(uid, str) else "",
        has_panels=_is_object_list(panels) and len(panels) > 0,
    )


# meiva:link cli/commands/observability/docs/lgtm/dashboard-provisioning.meiva.okf.md
def record_rendered_hash(
    hashes_path: Path, key: str, digest: str, *, source: str = "render"
) -> None:
    update = hash_record_update(
        _recorded_hashes(hashes_path), key=key, digest=digest, source=source
    )
    if update is None:
        return
    hashes_path.parent.mkdir(parents=True, exist_ok=True)
    hashes_path.write_text(json.dumps(update, indent=2) + "\n", encoding="utf-8")


# meiva:note
# Every provisioning file is copied verbatim, and the checkout owns the tree: a
# rendered copy that drifted from its recorded hash is restored and reported as
# converge_file_overwritten rather than preserved. The datasource and webhook
# URLs stay Grafana-native $__env{} references resolved from runtime.env, and
# Grafana reads the rendered tree through MEIVA_PROVISIONING_PATH, so a rollout
# that swaps the checkout cannot feed the running instance a half-rendered tree.
#
# The generated dashboard is skipped while the verbatim files are copied and rendered right
# after: the checkout copy is its reference output, not its source. It is never orphan-cleanup
# material, since it has no source template to vanish, so it stays in the rendered set even when
# generation fails and the previous file must survive.
# meiva:link cli/commands/converge/docs/rendered-files.meiva.okf.md
def render_provisioning(
    repo: Path,
    native_dir: Path,
    *,
    dashboard: DashboardOutcome,
    producer: Callable[[], EventPipeline],
) -> None:
    source_dir = repo / "deploy/lgtm/config/grafana/provisioning"
    if not source_dir.is_dir():
        return
    dest_dir = native_dir / "config" / "provisioning"
    hashes_path = native_dir / "config" / "provisioning-hashes.json"
    rendered_relative: set[str] = set()
    for source in sorted(source_dir.rglob("*")):
        if not source.is_file():
            continue
        rel = source.relative_to(source_dir)
        if rel.as_posix() == RENDERED_DASHBOARD_REL:
            continue
        rendered_relative.add(rel.as_posix())
        warning = write_rendered_guarded(
            dest_dir / rel,
            source.read_text(encoding="utf-8"),
            hashes_path,
            rel.as_posix(),
            surface="lgtm-provisioning",
            on_local_edit="overwrite",
        )
        if warning is not None:
            print(f"  ! lgtm native: {warning}", file=sys.stderr)
    rendered_relative.add(RENDERED_DASHBOARD_REL)
    _provision_dashboard(
        dest_dir / RENDERED_DASHBOARD_REL,
        hashes_path,
        RENDERED_DASHBOARD_REL,
        source_dir / RENDERED_DASHBOARD_REL,
        outcome=dashboard,
        producer=producer,
    )
    _remove_orphans(dest_dir, hashes_path, rendered_relative)
    _prune_ghost_hashes(dest_dir, hashes_path)


# meiva:note
# Remove rendered files whose source template vanished: an untouched copy is pure derived state,
# a copy the user edited is kept, loudly.
def _remove_orphans(dest_dir: Path, hashes_path: Path, rendered_relative: set[str]) -> None:
    for dest in sorted(dest_dir.rglob("*")):
        if not dest.is_file():
            continue
        rel = dest.relative_to(dest_dir).as_posix()
        if rel in rendered_relative:
            continue
        recorded = _recorded_hashes(hashes_path).get(rel)
        if orphan_removable(
            recorded=recorded if isinstance(recorded, str) else None,
            actual=hashlib.sha256(dest.read_bytes()).hexdigest(),
        ):
            dest.unlink()
            continue
        print(
            f"  ! lgtm native: rendered provisioning file {dest} has no source "
            "template anymore but was modified locally; kept",
            file=sys.stderr,
        )


# meiva:link cli/commands/observability/docs/lgtm/dashboard-provisioning.meiva.okf.md
def _prune_ghost_hashes(dest_dir: Path, hashes_path: Path) -> None:
    recorded = _recorded_hashes(hashes_path)
    present = frozenset(
        path.relative_to(dest_dir).as_posix() for path in dest_dir.rglob("*") if path.is_file()
    )
    ghosts = ghost_hash_keys(recorded, present=present)
    if not ghosts:
        return
    for key in ghosts:
        del recorded[key]
    hashes_path.write_text(json.dumps(recorded, indent=2) + "\n", encoding="utf-8")
    print(f"  lgtm native: dropped hash entries with no rendered file: {', '.join(ghosts)}")


def _recorded_hashes(hashes_path: Path) -> dict[str, object]:
    try:
        recorded: object = json.loads(hashes_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return recorded if _is_object_dict(recorded) else {}


def _grafana_get(url: str) -> GrafanaReply:
    try:
        with httpx.Client(trust_env=False, timeout=httpx.Timeout(2.0)) as client:
            response = client.get(url)
    except httpx.HTTPError as exc:
        return GrafanaReply(0, f"{type(exc).__name__}: {exc}")
    try:
        return GrafanaReply(response.status_code, response.json())
    except ValueError:
        return GrafanaReply(response.status_code, None)


# meiva:link cli/commands/observability/docs/lgtm/dashboard-provisioning.meiva.okf.md
def verify_dashboard_provisioning(native_dir: Path, *, grafana_url: str) -> list[str]:
    config_dir = native_dir / "config"
    dashboard_path = config_dir / "provisioning" / RENDERED_DASHBOARD_REL
    hashes_path = config_dir / "provisioning-hashes.json"
    return [
        *file_problems(str(dashboard_path), dashboard_document(dashboard_path)),
        *sidecar_problems(hashes_path.name, _recorded_hashes(hashes_path)),
        *grafana_problems(
            grafana_url,
            _grafana_get(f"{grafana_url}/api/dashboards/uid/{DASHBOARD_UID}"),
            _grafana_get(f"{grafana_url}/api/folders"),
        ),
    ]
