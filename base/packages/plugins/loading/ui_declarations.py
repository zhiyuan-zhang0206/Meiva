# meiva:note
# What the enabled plugins declare under `contributions.ui`, read from their manifests and stats faces.
# The console's aggregation endpoint and the status dashboard both ask it, so a status card is
# declared in one place and every reader agrees on which `plugin_stats` rows still have a card.
# meiva:link future/frontend-plugin-contributions.md

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.packages.plugins import enable_config
from base.packages.plugins.load_report import Reporter
from base.packages.plugins.loading.schedule_face import load_plugin_ui_stats_safe
from base.packages.plugins.manifest import ManifestError, load_manifest


# meiva:note
# An enabled plugin's `contributions.ui` object, with the directory its face files live in.
@dataclass(frozen=True)
class PluginUi:
    plugin: str
    plugin_dir: Path
    ui: dict[str, object]


# meiva:note
# The declaration half of a status card: identity and label. The value half is the `plugin_stats`
# row joined by `(plugin, id)`.
@dataclass(frozen=True)
class StatCard:
    plugin: str
    id: str
    label: str


# meiva:note
# An enabled plugin whose manifest no longer validates: the install gate already validated it, so
# the on-disk copy changed under the cluster. Raised, never skipped, so the plugin is named.
class InvalidPluginManifestError(Exception):
    def __init__(self, plugin: str, cause: ManifestError) -> None:
        super().__init__(f"plugin {plugin!r} has an invalid meiva-plugin.json: {cause}")
        self.plugin = plugin


def _dict_list(value: object) -> list[dict[str, object]]:
    return [item for item in value if is_object_dict(item)] if is_object_list(value) else []


# meiva:note
# Every enabled plugin that declares a `contributions.ui` object, in name order. The enabled set is
# the one the cluster's plugin policy materializes under `home`.
def enabled_plugin_ui(*, home: Path) -> list[PluginUi]:
    installed = enable_config.installed_plugin_dirs(home=home)
    config = enable_config.load_for_runtime(set(installed), home=home)
    declarations: list[PluginUi] = []
    for name in sorted(installed):
        plugin_dir = installed[name]
        entry = config.plugins.get(name)
        if entry is None or not entry.enabled:
            continue
        try:
            manifest = load_manifest(plugin_dir)
        except ManifestError as e:
            raise InvalidPluginManifestError(name, e) from e
        ui = manifest.contributions.get("ui")
        if is_object_dict(ui):
            declarations.append(PluginUi(name, plugin_dir, ui))
    return declarations


# meiva:note
# The cards one plugin declares: the manifest's `ui.stats` entries, then the `statsFace` ones a
# manifest entry of the same id does not already name. A face that fails to load is reported
# through `report` (the canonical loud reporter when None) and declares nothing.
def stat_cards(
    declaration: PluginUi, *, home: Path, report: Reporter | None = None
) -> list[StatCard]:
    cards: list[StatCard] = []
    seen_ids: set[str] = set()
    for card in _dict_list(declaration.ui.get("stats", [])):
        card_id = str(card["id"])
        seen_ids.add(card_id)
        cards.append(StatCard(declaration.plugin, card_id, str(card["label"])))
    if isinstance(declaration.ui.get("statsFace"), str):
        for spec in load_plugin_ui_stats_safe(
            declaration.plugin, declaration.plugin_dir, home=home, report=report
        ):
            if spec.id not in seen_ids:
                seen_ids.add(spec.id)
                cards.append(StatCard(declaration.plugin, spec.id, spec.label))
    return cards


# meiva:note
# The `(plugin, id)` of every card an enabled plugin declares: what a `plugin_stats` row must match
# to be shown. A reader that asks on every poll passes a quiet `report`, because the console's own
# read of the declarations already reports a face that fails to load.
def declared_stat_cards(
    *, home: Path, report: Reporter | None = None
) -> frozenset[tuple[str, str]]:
    return frozenset(
        (card.plugin, card.id)
        for declaration in enabled_plugin_ui(home=home)
        for card in stat_cards(declaration, home=home, report=report)
    )
