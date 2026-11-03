# meiva:note
# A handler that declares an option its parser never produced fails through `cli.main`'s dispatch.

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import pytest

from base.host.boundary.options import OptionsError, options
from cli import main as _main
from cli.database import open_operator_database


@dataclass(frozen=True)
class _Unbound:
    absent: str


# meiva:note
# A handler that declares an option its parser never produced raises before it acts, on the
# runtime path and on the init path alike: the mismatch is a programming error, not a usage error.
@pytest.mark.parametrize(
    "argv", [["status"], ["init", "--machine-name", "probe", "--serve-gateway"]]
)
def test_a_handler_the_namespace_does_not_fit_raises(argv: list[str], unit_home: Path) -> None:
    def _fake(args: argparse.Namespace) -> int:
        return options(args, _Unbound).absent.count("x")

    args = _main.parse_command_args(argv)
    args.func = _fake

    with pytest.raises(OptionsError, match="_Unbound: absent is missing"):
        _main.dispatch_parsed(
            args,
            argv,
            declared=unit_home,
            checkout=Path(_main.__file__).resolve().parents[1],
            producer_factory=_main.operator_producer,
            open_database=open_operator_database,
        )
