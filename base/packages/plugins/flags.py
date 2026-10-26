"""Plugin access to declared, non-sensitive core configuration flags.

Plugins declare their Core dependencies in ``default_config.py`` ``contribute()``:

    def contribute() -> PluginContributions:
        return PluginContributions(
            flags=("daemon.notice_ttl_limit_seconds",)
        )

Config-face admission and SDK installation validate every declared key. Services
read from the declaration itself, without a second process-wide registry:

    from base.packages.plugins.flags import read_declared_flag

    ttl = read_declared_flag("daemon.notice_ttl_limit_seconds", contribute().flags)

Declaration is mandatory: ``read_declared_flag`` rejects a key absent from the
supplied declaration. Keys are fully qualified as ``<domain>.<field>``. The namespace is
every non-sensitive core Settings field; secrets remain in their existing secret
channels and are never flags.

Reads use the process's constructed Settings domains. An unavailable domain
fails explicitly; a read never constructs a second Settings image.
"""

from base.packages.plugins.plugin_errors import (
    FlagDomainUnavailable as FlagDomainUnavailable,
)
from base.packages.plugins.plugin_errors import (
    PluginFlagError as PluginFlagError,
)
from base.packages.plugins.plugin_errors import (
    UndeclaredFlag as UndeclaredFlag,
)
from base.packages.plugins.plugin_errors import (
    UnknownFlag as UnknownFlag,
)
from base.packages.plugins.plugin_errors import (
    validate_flag_key as validate_flag_key,
)


def read_declared_flag(key: str, flags: tuple[str, ...]) -> object:
    """Read a service's explicit pure-face Core dependency without an SDK registry.

    Secrets remain resources. Unavailable profile domains fail rather than
    constructing a second full Settings image on a plugin's behalf.
    """
    for declared in flags:
        validate_flag_key(declared)
    if key not in flags:
        raise UndeclaredFlag(f"core flag {key!r} is absent from the pure config declaration")
    domain, field = key.split(".")
    from base.config import settings

    if not settings.has_domain(domain):
        raise FlagDomainUnavailable(f"declared core flag {key!r} is unavailable in this profile")
    return getattr(getattr(settings, domain), field)
