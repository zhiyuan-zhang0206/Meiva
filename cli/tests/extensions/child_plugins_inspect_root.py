# meiva:note
# Exercise the parser-bound inspect handler in a fresh process with a private plugin home.
# Its installed sampling owner must refuse another read after the verb returns.

from __future__ import annotations

import json
import sys
from pathlib import Path

from base import paths
from base.packages.plugins.enable_config import discover_plugin_dirs, write_local
from cli import parsers
from meiva.sdk_surface.installation import installed


def main(name: str) -> None:
    directory = paths.plugins_dir() / "inspect-root"
    directory.mkdir(parents=True)
    (directory / "plugin.py").write_text('"""An inspect-root fixture."""\nLOADED = True\n')
    (directory / "meiva-plugin.json").write_text(
        json.dumps(
            {
                "apiVersion": 2,
                "name": "inspect-root",
                "version": "1.0.0",
                "engines": {"meiva": ">=0.1.0"},
            }
        )
    )
    builtins = discover_plugin_dirs(
        repo_dir=paths.repo_plugins_dir(), user_dir=Path("/nonexistent")
    )
    write_local(
        {
            "plugins": {
                **{key: {"enabled": False} for key in builtins},
                "inspect-root": {"enabled": True},
            }
        }
    )
    args = parsers.parse_args(["plugins", "inspect", name], retained_children=[])
    rc = args.func(args)
    current = installed()
    assert current is not None
    try:
        current.sampling.read()
    except RuntimeError as exc:
        assert "has stopped" in str(exc)
    else:
        raise AssertionError("inspect returned without closing its sampling owner")
    assert current.sampling.worker is None or not current.sampling.worker.thread.is_alive()
    print("INSPECT_ROOT_RESULT " + json.dumps({"rc": rc, "stopped": True}))


if __name__ == "__main__":
    main(sys.argv[1])
