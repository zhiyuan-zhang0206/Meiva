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


@pytest.mark.parametrize(
    "statement",
    [
        'query = sql.SQL("UPDATE {} SET cost_usd = NULL").format(sql.Identifier("llm_usage_ledger"))\n'
        "conn.execute(query)",
        'conn.execute("UPDATE llm_usage_ledger SET cost_usd = NULL")',
        'query = "UPDATE llm_usage_ledger SET cost_usd = NULL"\nconn.execute(query)',
        "from psycopg import sql as pg\n"
        'conn.execute(pg.SQL("UPDATE {table} SET cost_usd = NULL").format('
        'table=pg.Identifier("public", "llm_usage_ledger")))',
    ],
)
def test_ledger_repricing_requires_the_owner(statement: str) -> None:
    path = "gateway/agents/history/inbound.py"
    assert f"{path}::usage-ledger-reprice" in decisions.owner_bypasses(
        ast.parse(statement), path, (), _TABLE
    )


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO {} VALUES (1)",
        "DELETE FROM {} WHERE call_id = %s",
        "MERGE INTO {} USING other ON false WHEN NOT MATCHED THEN INSERT VALUES (1)",
        "TRUNCATE TABLE {}",
        "COPY {} FROM STDIN",
    ],
)
def test_composed_ledger_mutations_require_the_owner(statement: str) -> None:
    path = "gateway/agents/history/inbound.py"
    source = f"cursor.execute(sql.SQL({statement!r}).format(sql.Identifier('llm_usage_ledger')))"
    assert decisions.owner_bypasses(ast.parse(source), path, (), _TABLE) == {
        f"{path}::usage-ledger-insert": [1]
    }


@pytest.mark.parametrize(
    "source",
    [
        "def ledger_owner_message():\n"
        '    return "Only the owner may run UPDATE llm_usage_ledger SET cost_usd = NULL"',
        'record_prompt("UPDATE llm_usage_ledger SET cost_usd = NULL")',
        'cursor.execute("INSERT INTO prompts VALUES (%s)", ("DELETE FROM llm_usage_ledger",))',
        'cursor.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier("llm_usage_ledger")))',
        'cursor.execute(sql.SQL("UPDATE other SET message = {}").format('
        'sql.Literal("UPDATE llm_usage_ledger SET cost_usd = NULL")))',
        'cursor.execute(sql.SQL("INSERT INTO {} SELECT * FROM {}").format('
        'sql.Identifier("archive"), sql.Identifier("llm_usage_ledger")))',
    ],
)
def test_inert_text_and_readonly_ledger_access_pass(source: str) -> None:
    path = "gateway/agents/history/inbound.py"
    assert decisions.owner_bypasses(ast.parse(source), path, (), _TABLE) == {}


@pytest.mark.parametrize(
    "source",
    [
        '(home / "run" / "bootstrap-mirror").with_suffix(".json").write_bytes(data)',
        'STEM = "bootstrap-mirror"\ntarget = home.joinpath("run", STEM).with_suffix(".json")\n'
        "os.replace(temp, target)",
        'STEM = "bootstrap-mirror"\nopen(home / "run" / (STEM + ".json"), "wb")',
        'STEM = "bootstrap-mirror"\n(home / f"{STEM}-enrolled").touch()',
    ],
)
def test_composed_mirror_paths_require_the_bootstrap_owner(source: str) -> None:
    path = "cli/commands/lifecycle/converge_host.py"
    tree = ast.parse(source)
    assert f"{path}::bootstrap-mirror" in decisions.owner_bypasses(tree, path, (), _TABLE)
    assert decisions.owner_bypasses(tree, "base/host/env/bootstrap.py", (), _TABLE) == {}


@pytest.mark.parametrize(
    "source",
    [
        'log.info("bootstrap-mirror")',
        '(home / "bootstrap-mirror-backup.json").write_bytes(data)',
        '(home / "bootstrap-mirror").with_suffix(".bak").write_bytes(data)',
    ],
)
def test_unrelated_mirror_paths_and_text_pass(source: str) -> None:
    assert (
        decisions.owner_bypasses(
            ast.parse(source), "cli/commands/lifecycle/converge_host.py", (), _TABLE
        )
        == {}
    )


@pytest.mark.parametrize(
    "source",
    [
        "sink = ReadTimelineSink()\nif isinstance(sink, ReadTimelineSink):\n    sink._overlay.retire_all()",
        "from agent.graph.claim.harness_read_sink import ReadTimelineSink as Sink\n"
        "alias = sink = Sink()\nalias._overlay.retire_all()",
        "projector.sink._projector.reset()",
        "sink = ReadTimelineSink(); sink._overlay.retire_all()",
        'getattr(projector.sink, "_overlay").retire_all()',
        'vars(projector.sink)["_overlay"].retire_all()',
    ],
)
def test_claim_cannot_reach_sink_private_state(source: str) -> None:
    path = _CLAIM + "harness_turn.py"
    assert runtime_boundaries.access_errors(path, ast.parse(source))


def test_claim_can_send_public_sink_notifications() -> None:
    path = _CLAIM + "harness_turn.py"
    source = (
        "sink = ReadTimelineSink()\nsink.on_event(event)\nprojector.sink.finish()\nother._overlay"
    )
    assert runtime_boundaries.access_errors(path, ast.parse(source)) == []
    assert (
        runtime_boundaries.access_errors(
            _CLAIM + "harness_read_sink.py", ast.parse("self._overlay.retire_all()")
        )
        == []
    )


@pytest.mark.parametrize("names", [(), ("build_timeline_items",), ("display_item_count",)])
def test_quote_cannot_import_legacy_display(names: tuple[str, ...]) -> None:
    fact = Fact(
        _ADMISSION + "quote_source.py",
        _ADMISSION + "legacy_quote.py",
        1,
        FactKind.IMPORT,
        names,
    )
    assert runtime_boundaries.import_errors([fact])


def test_legacy_validator_is_only_available_at_the_fallback() -> None:
    path = _ADMISSION + "quote_source.py"
    fact = Fact(
        path, _ADMISSION + "legacy_quote.py", 1, FactKind.IMPORT, ("validate_legacy_quote",)
    )
    assert runtime_boundaries.import_errors([fact]) == []
    entry = "from gateway.message_admission.legacy_quote import validate_legacy_quote as validate\n"
    assert (
        runtime_boundaries.access_errors(
            path, ast.parse(entry + "async def validate_quote_source():\n    await validate()")
        )
        == []
    )
    assert runtime_boundaries.access_errors(
        path, ast.parse(entry + "async def _contains_native_source():\n    await validate()")
    )


@pytest.mark.parametrize(
    "source",
    [
        'query = sql.SQL("UPDATE ") + sql.Identifier("llm_usage_ledger") + '
        'sql.SQL(" SET cost_usd = NULL WHERE call_id = %s")\nconn.execute(query, (call_id,))',
        'table = sql.Identifier("public", "llm_usage_ledger")\n'
        'parts = [sql.SQL("UPDATE "), table, sql.SQL(" SET cost_usd = NULL")]\n'
        "query = sql.Composed(parts)\ncursor.execute(query)",
        'parts = (sql.SQL("UPDATE"), sql.Identifier("llm_usage_ledger"), '
        'sql.SQL("SET cost_usd = NULL"))\n'
        'query = sql.SQL(" ").join(parts)\nconn.execute(query)',
        "from psycopg.sql import Composed as Composition, Identifier, SQL\n"
        'conn.execute(Composition([SQL("UPDATE "), Identifier("llm_usage_ledger"), '
        'SQL(" SET cost_usd = NULL")]))',
        'conn.execute(sql.SQL("{} {} SET cost_usd = NULL").format('
        'sql.SQL("UPDATE"), sql.Identifier("llm_usage_ledger")))',
        'conn.execute(sql.SQL("UPDATE ") + sql.SQL("") + '
        'sql.Identifier("llm_usage_ledger") + sql.SQL(" SET cost_usd = NULL"))',
    ],
)
def test_psycopg_composition_cannot_bypass_ledger_ownership(source: str) -> None:
    path = "gateway/agents/history/inbound.py"
    assert f"{path}::usage-ledger-reprice" in decisions.owner_bypasses(
        ast.parse(source), path, (), _TABLE
    )


@pytest.mark.parametrize(
    "query",
    [
        '''SELECT 1 AS "How to UPDATE llm_usage_ledger SET cost_usd"''',
        '''SELECT 1 AS "Examples ""DELETE FROM llm_usage_ledger"" here"''',
        """SELECT 'UPDATE llm_usage_ledger SET cost_usd = NULL'""",
        """SELECT $$UPDATE llm_usage_ledger SET cost_usd = NULL$$""",
        """SELECT $example$DELETE FROM llm_usage_ledger$example$""",
        """SELECT 1 AS "UPDATE" FROM llm_usage_ledger""",
        """UPDATE "sample UPDATE llm_usage_ledger SET cost_usd" SET field = NULL""",
    ],
)
def test_quoted_sql_text_cannot_trigger_ledger_ownership(query: str) -> None:
    path = "gateway/agents/history/inbound.py"
    assert decisions.owner_bypasses(ast.parse(f"conn.execute({query!r})"), path, (), _TABLE) == {}


def test_quoted_ledger_target_and_alias_require_the_owner() -> None:
    path = "gateway/agents/history/inbound.py"
    query = 'UPDATE "public"."llm_usage_ledger" AS "ledger alias" SET cost_usd = NULL'
    assert decisions.owner_bypasses(ast.parse(f"conn.execute({query!r})"), path, (), _TABLE) == {
        f"{path}::usage-ledger-reprice": [1]
    }
