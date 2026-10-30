# meiva:note
# An attribute of a module this process has already imported. Two states can be in
# `sys.modules` without being usable: the module never loaded (None) and the module
# another thread is still executing, whose attributes do not exist yet — touching one
# raises "partially initialized module ... has no attribute", which reads like a
# circular import but is a concurrent one. Only the second is skipped, because a
# module that has not finished importing has created no sink to quiesce (task #5860).
# A module that finished importing and no longer owns the attribute is a rename or a
# deletion, and stays an AttributeError: swallowing it would silently stop closing
# the pool the previous test left behind.
# meiva:link tests/fixtures/docs/native-provisioning.meiva.okf.md
from __future__ import annotations

import sys
from typing import Any

from base.log import logger


def loaded_attribute(module_name: str, attribute: str) -> Any | None:
    module = sys.modules.get(module_name)
    if module is None:
        return None
    spec = getattr(module, "__spec__", None)
    if spec is not None and getattr(spec, "_initializing", False):
        logger.debug("{} is still importing; its {} cannot be read yet", module_name, attribute)
        return None
    return getattr(module, attribute)
