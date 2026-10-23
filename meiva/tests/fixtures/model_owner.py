"""Explicit SDK model installation owned by consumer-local tests."""

from collections.abc import Iterator

import pytest

from base.config.service_read import ConfigAuthority
from base.lm.catalog import ModelCatalog
from base.packages.tests.model_catalog import inert_keys
from meiva.sdk_surface.installation import Installation
from meiva.sdk_surface.tests.installation_holder import held_installation

__all__ = ["model_installation", "sdk_model_owner"]


@pytest.fixture
def model_installation(
    model_catalog: ModelCatalog, config_authority: ConfigAuthority
) -> Installation:
    """Return an explicit SDK owner; the caller chooses when to install it."""
    return Installation.bare(
        catalog=model_catalog, provider_keys=inert_keys(model_catalog), authority=config_authority
    )


@pytest.fixture
def sdk_model_owner(model_installation: Installation) -> Iterator[Installation]:
    """Hold the model installation as the process SDK root for direct SDK consumers.

    Installation/rollback tests pass model facts to their real installer instead.
    This fixture is opt-in; it never supplies a catalog to an unbound production root.
    """
    with held_installation(model_installation) as held:
        yield held
