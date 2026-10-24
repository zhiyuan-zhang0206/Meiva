# meiva:note
# Project the declarations into alias metadata in a process that has no runtime settings image
# and has imported neither the full config models nor the panel metadata.

from __future__ import annotations

import sys


def declaration_projection() -> None:
    from ops.env_audit import load_alias_metadata

    metadata = load_alias_metadata()
    assert metadata["MEIVA_DB_URL"].sensitive is True
    assert "base.config._full" not in sys.modules
    assert "base.config.admin.metadata" not in sys.modules


if __name__ == "__main__":
    declaration_projection()
