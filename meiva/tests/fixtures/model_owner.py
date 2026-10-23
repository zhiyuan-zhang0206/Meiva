"""Explicit SDK model installation owned by consumer-local tests."""

from collections.abc import Iterator

import pytest

from base.config.service_read import ConfigAuthority
from base.lm.catalog import ModelCatalog
from base.packages.plugins.extensions import EMPTY
from meiva.sdk_surface import meiva_module
from meiva.sdk_surface.install import uninstall
from meiva.sdk_surface.installation import SLOT, Installation, Loading, installed, slot

__all__ = ["bind_installation", "model_installation", "sdk_model_owner"]


def bind_installation(value: Installation | Loading | None) -> Installation | Loading | None:
    prior = slot()
    if prior is not value and installed() is not None:
        uninstall()
    setattr(meiva_module(), SLOT, value)
    return prior


@pytest.fixture
def model_installation(
    model_catalog: ModelCatalog, config_authority: ConfigAuthority
) -> Installation:
    """Return an explicit SDK owner; the caller chooses when to install it."""
    return Installation(
        registry=EMPTY,
        expansions=(),
        wrap_layers={},
        skill_providers=(),
        metered=(),
        disabled=frozenset(),
        faces=False,
        undo=(),
        catalog=model_catalog,
        authority=config_authority,
    )


@pytest.fixture
def sdk_model_owner(model_installation: Installation) -> Iterator[Installation]:
    """Bind model facts for direct SDK consumers that do not exercise installation.

    Installation/rollback tests pass model facts to their real installer instead.
    This fixture is opt-in; it never supplies a catalog to an unbound production root.
    """
    prior = bind_installation(model_installation)
    try:
        yield model_installation
    finally:
        bind_installation(prior)
