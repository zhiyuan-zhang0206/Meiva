# Package primitives

`base/packages/` owns documentation parsing, package installation, plugin configuration
and the shared text-length budget primitive. Its component nodes describe the current
contracts and implementation.

`plugin_config_images.py` owns whole-image validation, revisions and the locked
atomic writer shared by plugin installation and the config admin API. It stays
independent of Settings and admin metadata so first-time plugin installation in
an execution child preserves the boot-lite config boundary.

First-time binding uses the writer's create-only CAS. When another boot creates
the image first, binding reads and validates that winning whole image before
publishing the plugin config. Binding also rereads an image created after its
initial missing-image read. Invalid images, disappearing files and unexpected
read/write errors still fail binding; the default writer never overwrites an
existing authority.

Configuration upgrades reconcile the declared schema in memory before the image is
rewritten; retired fields follow the schema merge's existing removal rule.

`composer_commands.py` owns the composer `/name` commands: where their files live, the
merged and sorted catalog, and how a message expands. `load_skill_names` scans the
converged skills tree (`~/.meiva/skills/` gated by `loadable_skill_names`) plus any
project-local provider roots through `skills/skill_tree.py` (which hands each root's entries to `skills/tree_builder.py`), while every command-discovery
function takes the `Skill` list (`skills/tree.py`) as an argument and `CommandCatalog` binds
one source of it (`load_skill_names` in the gateway and the ops runner, `meiva.skills.names`
in the agent host). The agent's claim node receives the catalog through
`MeivaContext.commands`.

Expansion preserves the typed command order and adds no chain policy or actor
framing. Explicit command files override a same-named skill command. Catalog
names use dash/colon spelling; matching also accepts legacy underscore/dot
spelling, while the expansion echoes the spelling supplied by the caller.
Only a registered command at a command boundary begins another segment; paths,
mid-sentence names and an unrecognized `/recap/review` token are ordinary text.

## Documented components

- [Composer commands](composer-commands.md) — Composer command discovery and expansion.
- [Text length budgets](text-budget.md) — Text-length budgets: CJK-aware measurement and the soft/hard verdict.
- [Install Registry (`installed.json`)](../extensions/docs/install_registry/install_registry.md) — Install Registry (`installed.json`).
- [Install Registry Write Path](../extensions/docs/write-path.md) — Install Registry Write Path.
- [Plugin Enable Config](../plugins/docs/enable_config.md) — Plugin Enable Config.
- [Plugin config images and agent views](../plugins/docs/plugin-config/plugin-config.md) — Plugin Config Images and Agent Views.
