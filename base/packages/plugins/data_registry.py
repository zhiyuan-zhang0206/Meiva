"""Load plugin metric faces and admit each declaration atomically.

The gateway inspector and Grafana dashboard supply load a plugin's `metrics.py`
without loading the agent runtime. Its `contribute()` returns the metrics to admit.
`load_declaration` reports load failures and validates the relevant manifest entry
while the plugin's tree exists. `build_data_registry` fills each metric's plugin
name, validates queries and rejects a declaration with a duplicate metric name.

A failed face leaves no partial registry state.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from base.host.boundary.untyped import object_of
from base.packages.plugins import load_report
from base.packages.plugins.extensions import ExtensionRegistry, PluginContributions
from base.packages.plugins.gate import check_manifest
from base.telemetry.metrics.plugin_metrics import DuplicateMetric, validate_spec_sql

# The manifest key owned by the metrics face.
METRICS_KEY = "metrics"


@dataclass(frozen=True)
class DeclaredFace:
    """One plugin's declaration from one data face, ready for admission."""

    plugin: str
    contributions: PluginContributions


def declaration_of(module: ModuleType) -> PluginContributions:
    """`module.contribute()`, refused unless it is a `PluginContributions`."""
    contribute = object_of(getattr, module, "contribute", None)
    if contribute is None:
        raise AttributeError(f"{module.__name__} has no contribute()")
    if not callable(contribute):
        raise TypeError(f"{module.__name__}.contribute is not callable")
    contributions = object_of(contribute)
    if not isinstance(contributions, PluginContributions):
        raise TypeError(
            f"{module.__name__}.contribute() returned {type(contributions).__name__}, "
            "not PluginContributions"
        )
    return contributions


def load_declaration(
    plugin: str,
    importer: Callable[[], ModuleType],
    gate: tuple[Path, str] | None = None,
) -> DeclaredFace | None:
    """Import one data face and take its declaration; None (reported) when that fails.

    `importer` does the import (by dotted name, or by file for an unpacked tree) so the caller owns
    where the module comes from and what it caches. `gate` is `(plugin directory, manifest key)` to
    check the plugin's `meiva-plugin.json` against the declaration, or None to skip the check. A
    failure is the plugin's: reported through `load_report`, never raised.
    """
    try:
        contributions = declaration_of(importer())
        if gate is not None:
            check_manifest(plugin, gate[0], contributions, (gate[1],))
        return DeclaredFace(plugin, contributions)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        load_report.report_plugin_load_failure(plugin, exc)
        return None


def _admitted(face: DeclaredFace, taken_metrics: dict[str, str]) -> PluginContributions:
    """The face's declaration, validated, with every spec's `plugin` filled. Raises on refusal."""
    plugin, contributions = face.plugin, face.contributions
    names: set[str] = set()
    for spec in contributions.metrics:
        holder = taken_metrics.get(spec.name)
        if spec.name in names or holder is not None:
            raise DuplicateMetric(
                f"metric {spec.name!r} already declared (by plugin {holder or plugin!r}) — "
                f"names are global, prefix with the plugin name."
            )
        names.add(spec.name)
        validate_spec_sql(spec)
    return dataclasses.replace(
        contributions,
        metrics=tuple(spec.model_copy(update={"plugin": plugin}) for spec in contributions.metrics),
    )


def build_data_registry(
    declared: Iterable[DeclaredFace | None],
) -> tuple[ExtensionRegistry, list[str]]:
    """(registry of the admitted declarations, names of the plugins refused), in input order.

    A refusal is reported through `load_report` and costs only that plugin; metric names are unique
    across the whole registry, first claimant wins.
    """
    admitted: list[tuple[str, PluginContributions]] = []
    refused: list[str] = []
    taken: dict[str, str] = {}
    for face in declared:
        if face is None:
            continue
        try:
            contributions = _admitted(face, taken)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            load_report.report_plugin_load_failure(face.plugin, exc)
            refused.append(face.plugin)
            continue
        for spec in contributions.metrics:
            taken[spec.name] = face.plugin
        admitted.append((face.plugin, contributions))
    return ExtensionRegistry(tuple(admitted)), refused
