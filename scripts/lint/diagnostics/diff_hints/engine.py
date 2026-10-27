# meiva:note
# The whole analysis as one pure function: the diff's changes plus the text of the files
# they leave behind in, the sites out. The shell supplies the values (it reads git);
# nothing here touches a file, a clock or the environment.
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from meizel.graph.model import AmbientKind
from scripts.lint.diagnostics.diff_hints.ambient import (
    new_root_sites,
    root_middle_layer,
    service_locator,
)
from scripts.lint.diagnostics.diff_hints.branches import wildcard_case
from scripts.lint.diagnostics.diff_hints.model import (
    FileChange,
    HintId,
    Importers,
    NewRoot,
    Roles,
    Site,
    Unit,
)
from scripts.lint.diagnostics.diff_hints.removed import deleted_tests
from scripts.lint.diagnostics.diff_hints.scope import parse_unit
from scripts.lint.diagnostics.diff_hints.seams import double_subclasses, patch_shapes, spy_assert
from scripts.lint.diagnostics.diff_hints.shapes import callable_bag, mutable_in_frozen
from scripts.lint.diagnostics.diff_hints.signatures import (
    any_collaborator,
    callable_default,
    lambda_dependency,
    none_fallback,
    protocol_outside_base,
)

Detector = Callable[[Unit], list[Site]]

_DETECTORS: tuple[Detector, ...] = (
    service_locator,
    callable_default,
    none_fallback,
    any_collaborator,
    protocol_outside_base,
    callable_bag,
    mutable_in_frozen,
    wildcard_case,
    patch_shapes,
    double_subclasses,
    spy_assert,
    lambda_dependency,
    root_middle_layer,
)


# meiva:note
# The sites a diff raises, ordered by path and line. `texts` maps a path to the text of
# that file after the change; a deleted file has none and only raises `deleted-test`.
def analyze(
    changes: tuple[FileChange, ...],
    texts: Mapping[str, str],
    roles: Roles,
    roots: Mapping[str, frozenset[AmbientKind]],
    importers: Mapping[str, Importers],
    new_roots: Sequence[NewRoot],
) -> list[Site]:
    sites: list[Site] = []
    declared_now = {new.path for new in new_roots}
    for change in changes:
        text = texts.get(change.path)
        unit = (
            parse_unit(
                change.path,
                text,
                change.added,
                roles,
                roots.get(change.path, frozenset()),
                importers.get(change.path, Importers((), ())),
            )
            if text is not None
            else None
        )
        if unit is not None:
            for detector in _DETECTORS:
                sites.extend(
                    site
                    for site in detector(unit)
                    if not (site.hint is HintId.ROOT_MIDDLE_LAYER and site.path in declared_now)
                )
    sites.extend(deleted_tests(changes, roles))
    sites.extend(new_root_sites(new_roots))
    return sorted(sites, key=lambda site: (site.path, site.line, site.hint.value))
