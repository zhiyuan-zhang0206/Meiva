from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from meizel.allowances import Policy
from meizel.frontend_deps import FRONTEND_DIR
from meizel.graph.model import MeizelError, Sources
from meizel.graph.universe import Universe
from meizel.language import fixtures
from meizel.language.typescript_compiler.config import load_ts_config
from meizel.language.typescript_compiler.decode import decode_batch
from meizel.language.typescript_compiler.model import TsScan
from meizel.lint_common import run_node_analyzer

TOOL_ROOT = Path(__file__).parents[3]


# meiva:link meizel/docs/checker/typescript.meiva.okf.md
def scan_batch(
    universe: Universe, contents: dict[str, str], policy: Policy, sources: Sources
) -> dict[str, TsScan]:
    if not contents:
        return {}
    config = load_ts_config(policy)
    options = asdict(config)
    options["impure_calls"] = sorted(config.impure_calls)
    options["impure_hooks"] = sorted(config.impure_hooks)
    request = {
        "root": str(universe.root),
        "files": sorted(universe.files),
        "sources": contents,
        "config": options,
        "tests": [path for path in contents if fixtures.is_test_file(path)],
        "ambient": {"names": sources.names, "calls": sources.calls},
    }
    producer = Path(__file__).with_name("producer.mjs")
    # meiva:note
    # The compiler comes from this tool's own frontend install, never the checked repository's: the
    # checker runs against repositories that have no frontend at all, and a tree beside the inspected
    # code would be one more thing that code could change under the analysis.
    # meiva:link meizel/docs/frontend-deps.meiva.okf.md
    output = run_node_analyzer(
        producer, json.dumps(request), frontend=TOOL_ROOT.joinpath(*FRONTEND_DIR)
    )
    try:
        return decode_batch(output, frozenset(contents), policy.reader_kinds)
    except (TypeError, ValueError) as error:
        raise MeizelError(f"invalid TypeScript compiler result: {error}") from error
