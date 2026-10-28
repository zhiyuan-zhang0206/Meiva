"""The failures plugin config registration, binding and overlay validation raise."""


class PluginConfigError(Exception):
    """Root of plugin config register / bind failures. Plugin author / CLI use this for coarse catch."""


class DuplicateRegistration(PluginConfigError):  # noqa: N818
    """A plugin's Config is bound twice in one install — a second `bind_plugin_config` for the
    same plugin."""


class SchemaDriftError(PluginConfigError):
    """Disk image field set inconsistent with plugin Config class — run
    `meiva plugins update` to auto-merge (write defaults for new fields, drop removed)."""


class InvalidConfigData(PluginConfigError):  # noqa: N818 — parent already has Error suffix, subclass describes specific scenario
    """Disk image JSON parse failure / Pydantic validation failure — someone
    edited a line by hand and broke it; framework does not silently repair,
    plugin author / user handles it."""


class InvalidConfigOverlay(PluginConfigError):  # noqa: N818
    """A config overlay failed validation; the message says which key and why.
    Nothing was spawned or queued — fix the dict and call again."""


# meiva:note
# Root of plugin core-flag declaration and read failures.
class PluginFlagError(Exception):
    pass


# meiva:note
# A declared key is malformed, unknown, or names a sensitive core field.
class UnknownFlag(PluginFlagError):  # noqa: N818
    pass


# meiva:note
# The current plugin tried to read a flag absent from its declaration.
class UndeclaredFlag(PluginFlagError):  # noqa: N818
    pass


# meiva:note
# The running process profile did not construct a declared flag's domain.
class FlagDomainUnavailable(PluginFlagError):  # noqa: N818
    pass
