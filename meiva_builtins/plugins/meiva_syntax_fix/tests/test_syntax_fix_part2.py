# meiva:note
# Tests for plugins/meiva_syntax_fix/plugin.py. Coverage: converting Chinese punctuation to ASCII,
# detecting missing imports, inserting import statements at the correct position, `ruff check
# --fix`, and the full `syntax_fix_before_exec` pipeline (Chinese punctuation, imports, ruff,
# compile).

from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from agent.state import AgentState
from base.log.tests.log_records import LogRecords
from meiva_builtins.plugins.meiva_syntax_fix.agent_runtime import (
    syntax_fix_before_exec,
)
from meiva_builtins.plugins.meiva_syntax_fix.tests.hook_support import (
    REPAIRED,
    REPAIRS_UNTERMINATED_STRING,
    make_runtime,
)

# --- _fix_chinese_punctuation ---


# --- _translate_outside_strings crash guard (gh-149183) ---


# --- _detect_missing_imports ---


# --- _insert_imports ---


# --- _ruff_fix ---


# --- _ruff_format ---


# --- ruff give-up logging (issue #159) ---
# A ruff pass that gives up must be visible: a timeout / OS error logs a
# warning with the budget, input size, and errno; a missing ruff logs once per
# process. The pass-through behavior itself is unchanged.


# --- _ruff_executable ---


# ============================================================================
# _fix_invalid_escapes
# ============================================================================


# ============================================================================
# syntax_fix_before_exec integration
# ============================================================================


# ============================================================================
# _is_stdlib_module
# ============================================================================


# LLM repair helpers
# ============================================================================


# ============================================================================
# Deterministic fixes tests (syntax-fix v3)
# ============================================================================


def _is_broken(code: str) -> bool:
    """True when `code` fails to compile -- used to assert a fixture is a
    genuine syntax error before a fixer is run against it."""
    try:
        compile(code, "<t>", "exec")
    except SyntaxError:
        return True
    return False


# ============================================================================
# trace-v2 syntax_fix events (task #792 group B)
# ============================================================================


class TestSyntaxFixEvents:
    """The before/after retention events: rough fix stores before only (after
    is replayable), lm fix stores before AND after (LLM rewrites are not
    replayable). Both go out as event="syntax_fix" loguru records whose extra
    lands in the event's `attributes`."""

    @staticmethod
    def _config() -> RunnableConfig:
        return {"configurable": {"thread_id": "7"}}

    @staticmethod
    def _state(code: str) -> AgentState:
        return AgentState(
            messages=[
                AIMessage(
                    content="",
                    tool_calls=[{"name": "execute_code", "args": {"code": code}, "id": "1"}],
                )
            ]
        )

    @staticmethod
    def _fix_events(loguru_records: LogRecords) -> list[dict[str, Any]]:
        return [
            record["extra"]
            for record in loguru_records.events("syntax_fix")
            if "fix_type" in record["extra"]
        ]

    async def test_rough_fix_emits_before_only(self, loguru_records: LogRecords):
        original = "print(1\uff0c2)"

        result = await syntax_fix_before_exec(self._state(original), make_runtime(), self._config())

        assert result is not None
        [ev] = self._fix_events(loguru_records)
        assert ev["fix_type"] == "rough"
        assert ev["before"] == original
        assert "after" not in ev
        assert ev["fixes"], "deterministic fixes must be listed"
        assert "chinese_punct" in ",".join(ev["fixes"])

    async def test_rough_fix_event_logger_payload_shape(self, loguru_records: LogRecords):
        """The real emit path: a loguru record with event='syntax_fix' whose
        extra carries fix_type / before / fixes — the shape the event's
        `attributes` stores."""
        from meiva_builtins.plugins.meiva_syntax_fix import agent_runtime as _plugin

        _plugin.emit_syntax_fix_event(
            fix_type="rough",
            before="print(1\uff0c2)",
            after=None,
            fixes=["chinese_punct(1)"],
            note="deterministic fixes applied",
        )
        [record] = loguru_records.events("syntax_fix")
        assert record["level"].name == "INFO"
        captured = record["extra"]
        assert captured["fix_type"] == "rough"
        assert captured["before"] == "print(1\uff0c2)"
        assert captured["fixes"] == ["chinese_punct(1)"]
        assert "after" not in captured

    async def test_lm_fix_emits_before_and_after(self, loguru_records: LogRecords):
        """An LLM repair records fix_type=lm with both before (the
        deterministic-fixed source the LLM saw) and after (the repair)."""
        broken = "x = 'unterminated\nprint(x)"

        result = await syntax_fix_before_exec(
            self._state(broken),
            make_runtime(llm_override=REPAIRS_UNTERMINATED_STRING),
            self._config(),
        )

        assert result is not None
        [ev] = self._fix_events(loguru_records)
        assert ev["fix_type"] == "lm"
        assert ev["before"] == broken  # no deterministic fix fired -> LLM saw the original
        assert ev["after"] == REPAIRED

    async def test_no_event_when_nothing_changed(self, loguru_records: LogRecords):
        """Clean code that compiles as-is emits no syntax_fix event — the event
        stream records mutations only."""
        runtime = make_runtime(syntax_fix_ruff_format=False)

        await syntax_fix_before_exec(self._state("x = 1\nprint(x)"), runtime, self._config())

        assert self._fix_events(loguru_records) == []
