# meiva:note
# Finite fresh-interpreter proofs of the CI command type's cold import boundaries.

import sys
from pathlib import Path


def main(mode: str, source: Path) -> None:
    if mode == "leaf":
        from scripts.ci.pull_requests import gh_process

        assert Path(gh_process.__file__).resolve() == source.resolve()
        assert not any(name.startswith("base.") for name in sys.modules)
    elif mode == "api":
        from scripts.ci.pull_requests import runs_api

        assert runs_api.FetchStats().api_requests == 0
        assert "base.config" not in sys.modules
        assert "base.log" not in sys.modules
        assert "base.telemetry" not in sys.modules
    else:
        raise ValueError(f"unknown cold import probe {mode!r}")
    assert "scripts.ci.pull_requests.export_process" not in sys.modules


if __name__ == "__main__":
    main(sys.argv[1], Path(sys.argv[2]))
