# meiva:note
# The service selection one `meiva start` names on its command line.
#
# Every start names its selection explicitly (`--all-services`, `--only-service`
# or `--disable-service`). Nothing is persisted: no later start reuses an earlier
# start's choice. What a start actually launched is the root's unit manifest
# (`services.supervision.meiva_root_glue.manifests.launched_services`).

from __future__ import annotations


def resolve_selection(
    available: set[str],
    *,
    only: tuple[str, ...],
    excluded: tuple[str, ...],
    all_services: bool,
) -> set[str]:
    # meiva:note
    # The services this start skips, from exactly one explicit selection form.
    forms = sum((bool(only), bool(excluded), all_services))
    if forms != 1:
        raise ValueError(
            "name exactly one service selection: --all-services, --only-service or "
            "--disable-service"
        )
    requested = set(only) | set(excluded)
    if requested - available:
        raise ValueError("unknown service selection: " + ", ".join(sorted(requested - available)))
    if only:
        return available - set(only)
    return set(excluded)
