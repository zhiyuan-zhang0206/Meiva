# meiva:note
# `meiva update` — cluster-wide source-mode rollout over SSH (`run`, `down`, `up`).
#
# `populate_update_parser` builds the `run`/`down`/`up` subparsers with every choice optional at
# the argparse level so `validate_parsed` can report all missing or conflicting choices at once
# before touching any host. `cli.main` binds `UpdateWindowInputs` before dispatching `_h_update`,
# keeping `meiva update` settings-free and independent of the caller's local `$MEIVA_HOME`.

from __future__ import annotations

import argparse

from base.host.boundary.options import attribute


def _h_update(args: argparse.Namespace) -> int:
    from cli.fleet_update import UpdateWindowInputs, run_parsed

    return run_parsed(args, attribute(args, "update_inputs", UpdateWindowInputs))


def add_update_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from cli.fleet_update.choices import UPDATE_DESCRIPTION, populate_update_parser

    update_p = sub.add_parser(
        "update",
        description=UPDATE_DESCRIPTION,
        help="[cluster] update a networked source-mode cluster over SSH (run / down / up); "
        "every choice is explicit",
    )
    populate_update_parser(update_p, handler=_h_update)
