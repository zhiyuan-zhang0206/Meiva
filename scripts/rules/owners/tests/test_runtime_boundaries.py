import ast
from pathlib import Path

import pytest
from meizel.api import Fact, FactKind, GitRepo, Universe, allowances, analyze, tracked_files

from base.host.proc import run_bounded
from scripts.rules.owners import decisions, runtime_boundaries

_CLAIM = "agent/graph/claim/"
_ADMISSION = "gateway/message_admission/"
_HISTORY = "base/agents/history/"
_USAGE = "base/lm/usage/"
_POLICY = """
[sections.single-owner]
postgres-dial-owners = "names"
"*" = "reasons"
usage-ledger-insert-owners = "names"
usage-ledger-reprice-owners = "names"
usage-ledger-writer-owners = "names"
bootstrap-mirror-owners = "names"

[single-owner]
usage-ledger-insert-owners = ["base/db/usage_ledger.py"]
usage-ledger-reprice-owners = ["services/upkeep/events_maintenance/ledger_repricing.py"]
usage-ledger-writer-owners = ["base/lm/usage/__init__.py", "base/lm/usage/receipt.py"]
bootstrap-mirror-owners = ["base/host/env/bootstrap.py"]
"""
_TABLE = decisions.decision_table(allowances.parse(_POLICY)[0])


@pytest.mark.parametrize(
    ("src", "dst", "names", "repair"),
    [
        (_CLAIM + "harness_run.py", _HISTORY + "timeline.py", (), "ReadTimelineSink"),
        (
            _CLAIM + "harness_turn.py",
            _CLAIM + "harness_read_sink.py",
            ("SessionFileLookup",),
            "ReadTimelineSink",
        ),
        (
            _CLAIM + "harness_interrupt.py",
            _HISTORY + "transcript_timeline/record/read.py",
            (),
            "ReadTimelineSink",
        ),
        (_ADMISSION + "quote_source.py", _HISTORY + "timeline.py", (), "canonical source"),
        (
            _ADMISSION + "quote_source.py",
            _HISTORY + "transcript_timeline/__init__.py",
            ("record_identity",),
            "canonical source",
        ),
        (
            "gateway/routers/memory.py",
            "base/db/usage_ledger.py",
            ("insert_usage",),
            "usage receipts",
        ),
        (
            "services/derived/labeler/daemon.py",
            "base/db/usage_ledger.py",
            ("usage_writer",),
            "base/db/usage_ledger.py",
        ),
        (
            _CLAIM + "harness_activity.py",
            _USAGE + "__init__.py",
            ("log_usage_fields",),
            "usage receipts",
        ),
        (
            _ADMISSION + "quote_source.py",
            _USAGE + "__init__.py",
            ("log_usage_from_message",),
            "usage receipts",
        ),
    ],
)
def test_forbidden_dependencies_point_to_the_replacement(
    src: str, dst: str, names: tuple[str, ...], repair: str
) -> None:
    fact = Fact(src, dst, 17, FactKind.IMPORT, names)

    [error] = runtime_boundaries.import_errors([fact])

    assert error.startswith(f"{src}:17:")
    assert repair in error


@pytest.mark.parametrize(
    ("src", "dst", "names"),
    [
        (_CLAIM + "harness_turn.py", _CLAIM + "harness_read_sink.py", ("ReadTimelineSink",)),
        (
            _CLAIM + "harness_turn.py",
            _HISTORY + "transcript_timeline/record/harness.py",
            ("session_file",),
        ),
        (_CLAIM + "harness_read_sink.py", _HISTORY + "timeline_item.py", ("TimelineItem",)),
        (
            _ADMISSION + "quote_source.py",
            _HISTORY + "transcript_timeline/__init__.py",
            ("record_quote_coordinates",),
        ),
        (_ADMISSION + "legacy_quote.py", _HISTORY + "timeline.py", ("build_timeline_items",)),
        (
            _ADMISSION + "quote_source.py",
            _HISTORY + "native_source.py",
            (),
        ),
        ("gateway/app.py", "base/db/usage_ledger.py", ("bind_usage_ledger",)),
        (
            "gateway/agents/reply_suggestion/service.py",
            _USAGE + "__init__.py",
            ("log_usage_from_message",),
        ),
        (
            _CLAIM + "harness_activity.py",
            _USAGE + "receipt.py",
            ("log_receipted_usage_fields",),
        ),
    ],
)
def test_boundary_contracts_remain_available(src: str, dst: str, names: tuple[str, ...]) -> None:
    assert runtime_boundaries.import_errors([Fact(src, dst, 1, FactKind.IMPORT, names)]) == []


@pytest.mark.parametrize(
    ("path", "source", "decision"),
    [
        (
            "gateway/routers/memory.py",
            'conn.execute("INSERT INTO public.llm_usage_ledger VALUES (1)")',
            "usage-ledger-insert",
        ),
        (
            "gateway/routers/memory.py",
            """conn.execute('WITH bill AS (SELECT 1) UPDATE "llm_usage_ledger" SET cost_usd = 1')""",
            "usage-ledger-reprice",
        ),
        (
            _CLAIM + "harness_activity.py",
            "enqueue(ledger.write_usage_row)",
            "usage-ledger-writer",
        ),
        (
            "cli/commands/lifecycle/recover.py",
            'writer.write_bytes(home / "run/bootstrap-mirror.json", data)',
            "bootstrap-mirror",
        ),
        (
            "cli/commands/lifecycle/recover.py",
            '(home / "bootstrap-mirror-enrolled").unlink()',
            "bootstrap-mirror",
        ),
    ],
)
def test_external_state_bypasses_fail(path: str, source: str, decision: str) -> None:
    assert decisions.owner_bypasses(ast.parse(source), path, (), _TABLE) == {
        f"{path}::{decision}": [1]
    }


@pytest.mark.parametrize(
    ("path", "source"),
    [
        ("base/db/usage_ledger.py", 'sql.SQL("INSERT INTO llm_usage_ledger VALUES (1)")'),
        (
            "services/upkeep/events_maintenance/ledger_repricing.py",
            'conn.execute("UPDATE llm_usage_ledger SET cost_usd = 1")',
        ),
        (_USAGE + "__init__.py", "ledger.write_usage_row(entry)"),
        (_USAGE + "receipt.py", "ledger.write_usage_row(entry)"),
        ("base/host/env/bootstrap.py", 'home / "run" / "bootstrap-mirror.json"'),
        ("gateway/cluster/status.py", 'conn.execute("SELECT * FROM llm_usage_ledger")'),
        ("gateway/cluster/status.py", 'cursor.copy("COPY llm_usage_ledger TO STDOUT")'),
        (
            "gateway/cluster/status.py",
            'conn.execute("INSERT INTO llm_usage_ledger_archive VALUES (1)")',
        ),
        ("agent/graph/claim/harness_activity.py", "receipt.write(entry)"),
    ],
)
def test_owners_and_readers_pass(path: str, source: str) -> None:
    assert decisions.owner_bypasses(ast.parse(source), path, (), _TABLE) == {}


@pytest.mark.parametrize(
    ("src", "target", "violation"),
    [
        ("memory_search/app.py", "memory_search.store", False),
        ("memory_search/app.py", "memory_search.cache", True),
        ("embeddinggemma/app.py", "embeddinggemma.scheduler", False),
        ("embeddinggemma/engine.py", "embeddinggemma.scheduler", True),
        ("memory_search/store.py", "memory_indexer.backends.base", False),
        ("memory_search/store.py", "memory_indexer.backends.numpy", True),
        ("memory_indexer/embeddings/factory.py", "embeddinggemma.contract", False),
        ("memory_indexer/embeddings/factory.py", "embeddinggemma.engine", True),
    ],
)
def test_derived_service_uses_are_enforced_by_meizel(
    tmp_path: Path, pytestconfig: pytest.Config, src: str, target: str, violation: bool
) -> None:
    init = run_bounded(["git", "init", "--quiet", str(tmp_path)], timeout=10, capture_output=True)
    assert init.returncode == 0, init.stderr
    _ = (tmp_path / "meizel.toml").write_text(
        '[roles]\napplication = ["services"]\n', encoding="utf-8"
    )
    prefix = "services/derived/"
    checkout = pytestconfig.rootpath
    paths = [path for path in tracked_files(checkout) if path.startswith(prefix)]
    for rel in paths:
        if rel.endswith("/meizel.toml"):
            file = tmp_path / rel
            file.parent.mkdir(parents=True, exist_ok=True)
            _ = file.write_bytes((checkout / rel).read_bytes())
    mutant = tmp_path / (prefix + src)
    _ = mutant.write_text(f"import services.derived.{target}\n", encoding="utf-8")
    result = analyze(
        Universe(GitRepo(tmp_path), ["meizel.toml", *paths]), changed={prefix + src}, jobs=1
    )

    assert any(fact.src == prefix + src for fact in result.facts)
    assert any(d.code == "undeclared-edge" for d in result.verdict) == violation
