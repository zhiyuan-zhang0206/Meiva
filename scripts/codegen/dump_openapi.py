"""Dump the FastAPI app's OpenAPI spec to JSON — used by the frontend
`openapi-typescript` codegen (single source of truth; when backend Pydantic
schemas change, the frontend types-generated.ts also needs codegen to sync).

Usage:
    .venv/bin/python scripts/codegen/dump_openapi.py [out_path]

Default writes to `ui/web/openapi.json`. The npm `codegen:types` script
invokes this and then runs openapi-typescript to convert the JSON to TS.

import does not trigger lifespan (FastAPI app instantiation), so the DB
pool does not come up — pure schema extraction.
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

# Put project root on sys.path so the explicit gateway app factory can be imported.
# Same pattern as scripts/entrypoints/gateway.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from base.host.env.boot_actions import enter_scratch_home

if __name__ == "__main__":
    enter_scratch_home()

from base.host.boundary.json_values import is_object_dict

# meiva.self.AGENT_ID stays None here — fine; dump is a build-time tool that
# does not exercise the SDK or make any gateway HTTP calls.


def _product_version() -> str:
    pyproject = Path(__file__).resolve().parent.parent.parent / "pyproject.toml"
    version = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    if not isinstance(version, str):
        raise TypeError(f"{pyproject} [project].version is not a string")
    return version


# meiva:note
# The document's `paths` object, or an empty one when the document has none.
def _paths(spec: dict[str, object]) -> dict[str, object]:
    paths = spec.get("paths")
    return paths if is_object_dict(paths) else {}


def main() -> int:
    from gateway.app import openapi_schema

    spec = openapi_schema()
    # meiva:note
    # FastAPI defaults info.version to 0.1.0; the checked-in contract carries
    # the product version instead so the document has one version answer.
    info = spec["info"]
    if not is_object_dict(info):
        raise TypeError("the OpenAPI document has no info object")
    info["version"] = _product_version()
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "ui/web/openapi.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    # Trailing newline so the pre-commit `end-of-file-fixer` hook stops
    # touching this generated file (POSIX text file convention + reduces
    # noise of "false drift" from the codegen-fresh hook).
    out.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"OpenAPI spec -> {out} ({len(_paths(spec))} paths)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
