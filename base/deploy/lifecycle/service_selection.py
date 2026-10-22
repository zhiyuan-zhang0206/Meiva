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


# meiva:note
# Retiring extras retires the permissions helper, which is the parent of the service root: the
# root exits only when no unit stays behind for it to host. A stop that keeps a unit therefore
# cannot retire the helper, and asking for both stops every unit before it fails — leaving the
# host down with the helper still loaded (mba, 2026-10-22). Stated once here so the command, the
# fleet's own parser and any future caller refuse the same pairs with the same words.
KEPT_WITH_EXTRAS: tuple[tuple[str, str], ...] = (
    ("shell", "--keep-shell-sessions"),
    ("browser", "--keep-browser"),
    ("kept", "--keep-service"),
)


# meiva:note
# The keep flags a parsed command chose, named as the operator typed them.
def kept_with_extras(choices: object) -> list[str]:
    return [flag for name, flag in KEPT_WITH_EXTRAS if getattr(choices, name, None) == flag]


# meiva:note
# Why this stop cannot retire extras, or None when it can.
def extras_keep_refusal(choices: object) -> str | None:
    kept = kept_with_extras(choices)
    if getattr(choices, "extras", None) != "--stop-extras" or not kept:
        return None
    return (
        "--stop-extras retires the permissions helper and needs the service root stopped, so "
        f"nothing may be kept; conflicting: {', '.join(kept)}. Drop them, or use --keep-extras"
    )
