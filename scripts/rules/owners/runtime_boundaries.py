# meiva:link scripts/rules/docs/rules.md

import ast
import re
from collections.abc import Iterable, Iterator, Mapping
from pathlib import PurePosixPath
from string import Formatter

from meizel.api import Fact, lint_common

_CLAIM = "agent/graph/claim/"
_ADMISSION = "gateway/message_admission/"
_QUOTE_SOURCE = _ADMISSION + "quote_source.py"
_LEGACY_QUOTE = _ADMISSION + "legacy_quote.py"
_LEGACY_VALIDATOR = "validate_legacy_quote"
_HISTORY = "base/agents/history/"
_TRANSCRIPT = _HISTORY + "transcript_timeline/"
_SINK = _CLAIM + "harness_read_sink.py"
_USAGE = "base/lm/usage/"
_LEDGER = "base/db/usage_ledger.py"

_TIMELINE = frozenset({_HISTORY + "timeline.py", _HISTORY + "timeline_item.py"})
_RECORD_LOCATION = frozenset({"record_location", "session_file", "unreadable_file"})
_PROJECTION_EVENTS = frozenset({"TimelineSnapshot", "InboundCommitted"})


def _claim_projection(fact: Fact) -> bool:
    if fact.dst == _SINK:
        return fact.src != _CLAIM + "harness_turn.py" or fact.names != ("ReadTimelineSink",)
    if fact.src in {_SINK, _CLAIM + "_present.py"}:
        return False
    if fact.dst == _TRANSCRIPT + "record/harness.py" and fact.src == _CLAIM + "harness_turn.py":
        return not fact.names or not _RECORD_LOCATION.issuperset(fact.names)
    if fact.dst == "base/events/live/projection.py":
        return bool(_PROJECTION_EVENTS.intersection(fact.names))
    return (
        fact.dst in _TIMELINE
        or fact.dst.startswith(_TRANSCRIPT)
        or fact.dst == "agent/graph/llm_stream/harness_overlay.py"
    )


def _admission_projection(fact: Fact) -> bool:
    if fact.dst == _LEGACY_QUOTE:
        return fact.src != _QUOTE_SOURCE or fact.names != (_LEGACY_VALIDATOR,)
    if fact.src == _LEGACY_QUOTE and fact.dst in _TIMELINE:
        return False
    if fact.dst == _TRANSCRIPT + "__init__.py":
        return fact.names != ("record_quote_coordinates",)
    return (
        fact.dst in _TIMELINE
        or fact.dst.startswith(_TRANSCRIPT)
        or fact.dst.startswith("gateway/agents/history/")
    )


def _direct_usage(fact: Fact) -> bool:
    if fact.dst == _LEDGER:
        return fact.src != "gateway/app.py" or fact.names != ("bind_usage_ledger",)
    if fact.dst == "agent/llm/usage.py":
        return True
    if fact.dst != _USAGE + "__init__.py":
        return False
    sync_emitters = {"log_usage_fields", "log_usage_from_message"}
    if fact.src == "gateway/agents/reply_suggestion/service.py":
        sync_emitters.remove("log_usage_from_message")
    return bool(sync_emitters.intersection(fact.names))


def _repair(fact: Fact) -> str | None:
    if lint_common.is_test_path(fact.src):
        return None
    if (fact.src.startswith(_CLAIM) or fact.dst == _SINK) and _claim_projection(fact):
        return "keep timeline projection in the notification-only ReadTimelineSink"
    if (fact.src.startswith(_ADMISSION) or fact.dst == _LEGACY_QUOTE) and _admission_projection(
        fact
    ):
        return "verify quote membership through the canonical source identity contracts in quote_source.py"
    if fact.dst == _LEDGER and {"insert_usage", "usage_writer"}.intersection(fact.names):
        return "keep ledger inserts in base/db/usage_ledger.py and submit usage receipts on request paths"
    if fact.src.startswith((_CLAIM, "gateway/")) and _direct_usage(fact):
        return "submit usage receipts through the service-owned staging runtime"
    return None


def import_errors(facts: Iterable[Fact]) -> list[str]:
    return sorted(
        {
            f"{fact.src}:{fact.line}: {fact.dst} crosses a runtime boundary; {repair}."
            for fact in facts
            if (repair := _repair(fact)) is not None
        }
    )


_SQL_LEDGER = r'(?:["\w]+\s*\.\s*)?"?llm_usage_ledger"?(?![\w"])'
_LEDGER_INSERT = re.compile(
    rf"\b(?:INSERT\s+INTO\s+|DELETE\s+FROM\s+|MERGE\s+INTO\s+|TRUNCATE\s+(?:TABLE\s+)?)"
    rf"(?:ONLY\s+)?{_SQL_LEDGER}(?=\s|\(|;|$)"
    rf"|\bCOPY\s+{_SQL_LEDGER}(?:\s*\([^)]*\))?\s+FROM\b",
    re.IGNORECASE,
)
_LEDGER_REPRICE = re.compile(
    rf"\bUPDATE\s+(?:ONLY\s+)?{_SQL_LEDGER}\s+(?:SET\b|(?:AS\s+)?\w+\s+SET\b)",
    re.IGNORECASE,
)


_SCOPE = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
type _Bindings = Mapping[str, ast.expr | None]


def _scope_nodes(node: ast.AST) -> Iterator[ast.AST]:
    for child in ast.iter_child_nodes(node):
        yield child
        if not isinstance(child, _SCOPE):
            yield from _scope_nodes(child)


def _assigned_values(node: ast.AST) -> Iterator[tuple[str, ast.expr | None]]:
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name):
                yield target.id, node.value
    if isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and isinstance(node.target, ast.Name):
        yield node.target.id, node.value
    if isinstance(node, ast.arg):
        yield node.arg, None


def _scoped_nodes(scope: ast.AST, inherited: _Bindings) -> Iterator[tuple[ast.AST, _Bindings]]:
    nodes = tuple(_scope_nodes(scope))
    local: dict[str, ast.expr | None] = {}
    for node in nodes:
        for name, value in _assigned_values(node):
            local[name] = None if name in local else value
    bindings = {**inherited, **local}
    for node in nodes:
        yield node, bindings
        if isinstance(node, _SCOPE):
            yield from _scoped_nodes(node, bindings)


def _bound_value(node: ast.expr, bindings: _Bindings) -> ast.expr:
    while isinstance(node, ast.Name):
        value = bindings.get(node.id)
        if value is None or (value.lineno, value.col_offset) >= (node.lineno, node.col_offset):
            break
        node = value
    return node


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _dotted_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted_name(node.value)}.{node.attr}"
    return ""


def _text(node: ast.expr, bindings: _Bindings) -> str | None:
    node = _bound_value(node, bindings)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _text(node.left, bindings), _text(node.right, bindings)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.JoinedStr):
        return "".join(_text(part, bindings) or "?" for part in node.values)
    if isinstance(node, ast.FormattedValue) and node.conversion == -1 and node.format_spec is None:
        return _text(node.value, bindings)
    return None


def _names_from(nodes: Iterable[ast.AST], module: str, symbol: str) -> set[str]:
    return {
        name.asname or name.name
        for node in nodes
        if isinstance(node, ast.ImportFrom) and node.module == module
        for name in node.names
        if name.name == symbol
    }


def _sink_expression(node: ast.expr, bindings: _Bindings, constructors: set[str]) -> bool:
    node = _bound_value(node, bindings)
    if isinstance(node, ast.Call):
        return isinstance(node.func, ast.Name) and node.func.id in constructors
    if isinstance(node, ast.IfExp):
        return _sink_expression(node.body, bindings, constructors) or _sink_expression(
            node.orelse, bindings, constructors
        )
    return isinstance(node, ast.Attribute) and node.attr == "sink"


def _private_receiver(node: ast.AST, bindings: _Bindings) -> ast.expr | None:
    if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
        return node.value
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id == "vars" and node.args:
            return node.args[0]
        if node.func.id in {"getattr", "setattr", "delattr", "hasattr"} and len(node.args) >= 2:
            attr = _text(node.args[1], bindings)
            if attr and attr.startswith("_"):
                return node.args[0]
    return None


def _sink_lines(tree: ast.Module) -> set[int]:
    nodes = tuple(ast.walk(tree))
    constructors = (
        _names_from(nodes, "agent.graph.claim.harness_read_sink", "ReadTimelineSink")
        | _names_from(nodes, "harness_read_sink", "ReadTimelineSink")
        | {"ReadTimelineSink"}
    )
    constructed = {id(node.func) for node in nodes if isinstance(node, ast.Call)}
    return {
        node.lineno
        for node, bindings in _scoped_nodes(tree, {})
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in constructors
            and id(node) not in constructed
        )
        or (
            isinstance(node, (ast.Attribute, ast.Call))
            and (receiver := _private_receiver(node, bindings)) is not None
            and _sink_expression(receiver, bindings, constructors)
        )
    }


def _legacy_quote_lines(tree: ast.Module) -> set[int]:
    nodes = tuple(ast.walk(tree))
    aliases = _names_from(nodes, "gateway.message_admission.legacy_quote", _LEGACY_VALIDATOR)
    aliases |= _names_from(nodes, "legacy_quote", _LEGACY_VALIDATOR)
    allowed = {
        id(stmt.value.value.func)
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "validate_quote_source"
        and isinstance(stmt := node.body[-1], ast.Expr)
        and isinstance(stmt.value, ast.Await)
        and isinstance(stmt.value.value, ast.Call)
    }
    return {
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id in aliases
        and id(node) not in allowed
    }


def access_errors(rel: str, tree: ast.Module) -> list[str]:
    if lint_common.is_test_path(rel):
        return []
    if rel.startswith(_CLAIM) and rel != _SINK:
        return [
            f"{rel}:{line}: use only the public notification methods of ReadTimelineSink."
            for line in sorted(_sink_lines(tree))
        ]
    if rel == _QUOTE_SOURCE:
        return [
            f"{rel}:{line}: call validate_legacy_quote only at the legacy cursor fallback."
            for line in sorted(_legacy_quote_lines(tree))
        ]
    return []


_SQL_EXECUTION = frozenset({"execute", "executemany", "copy"})
_SQL_QUOTED = re.compile(
    r"""(?P<identifier>"(?:[^"]|"")*")"""
    r"""|(?i:e)'(?:[^'\\]|\\.|'')*'|'(?:''|[^'])*'"""
    r"""|(?P<dollar>\$(?:[a-zA-Z_][a-zA-Z_0-9]*)?\$).*?(?P=dollar)"""
    r"""|--[^\n]*|/\*.*?\*/""",
    re.DOTALL,
)


def _sql_token(match: re.Match[str]) -> str:
    if match.group("identifier") is None:
        return " "
    if match[0] == '"llm_usage_ledger"':
        return match[0]
    return "quoted_identifier"


def _sql_names(tree: ast.Module, symbol: str) -> set[str]:
    nodes = tuple(ast.walk(tree))
    modules = {"sql", "psycopg.sql"} | _names_from(nodes, "psycopg", "sql")
    for node in nodes:
        if isinstance(node, ast.Import):
            modules.update(
                alias.asname or alias.name for alias in node.names if alias.name == "psycopg.sql"
            )
            modules.update(
                f"{alias.asname}.sql"
                for alias in node.names
                if alias.name == "psycopg" and alias.asname
            )
    return {f"{module}.{symbol}" for module in modules} | _names_from(nodes, "psycopg.sql", symbol)


class _SQLExpressions:
    def __init__(self, tree: ast.Module) -> None:
        self.constructors = {
            name: symbol
            for symbol in ("SQL", "Identifier", "Composed")
            for name in _sql_names(tree, symbol)
        }

    def render(self, node: ast.expr, bindings: _Bindings) -> str | None:
        value = _bound_value(node, bindings)
        if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
            left = self.render(value.left, bindings)
            right = self.render(value.right, bindings)
            if left is None and right is None:
                return None
            return (left if left is not None else "?") + (right if right is not None else "?")
        if isinstance(value, ast.Call):
            return self._call(value, bindings)
        return None

    def _call(self, node: ast.Call, bindings: _Bindings) -> str | None:
        kind = self.constructors.get(_dotted_name(node.func))
        if kind == "Identifier":
            return ".".join(self._identifier(arg, bindings) for arg in node.args)
        if kind == "SQL" and node.args:
            return _text(node.args[0], bindings)
        if kind == "Composed" and node.args:
            return self._join("", node.args[0], bindings)
        if isinstance(node.func, ast.Attribute):
            return self._method(node, node.func, bindings)
        return None

    @staticmethod
    def _identifier(node: ast.expr, bindings: _Bindings) -> str:
        text = _text(node, bindings)
        return '"' + (text if text is not None else "?").replace('"', '""') + '"'

    def _method(self, node: ast.Call, method: ast.Attribute, bindings: _Bindings) -> str | None:
        if method.attr not in {"format", "join"}:
            return None
        template = self.render(method.value, bindings)
        if template is None:
            return None
        if method.attr == "format":
            return self._format(template, node, bindings)
        return self._join(template, node.args[0], bindings) if node.args else None

    def _fragment(self, node: ast.expr, bindings: _Bindings) -> str:
        text = self.render(node, bindings)
        return text if text is not None else "?"

    def _join(self, separator: str, node: ast.expr, bindings: _Bindings) -> str | None:
        value = _bound_value(node, bindings)
        if not isinstance(value, (ast.List, ast.Tuple)):
            return None
        return separator.join(self._fragment(element, bindings) for element in value.elts)

    def _format(self, template: str, call: ast.Call, bindings: _Bindings) -> str | None:
        fields = {str(index): self._fragment(arg, bindings) for index, arg in enumerate(call.args)}
        fields.update(
            {
                kw.arg: self._fragment(kw.value, bindings)
                for kw in call.keywords
                if kw.arg is not None
            }
        )
        parts: list[str] = []
        position = 0
        try:
            for literal, field, _, _ in Formatter().parse(template):
                parts.append(literal)
                if field == "":
                    parts.append(fields.get(str(position), "?"))
                    position += 1
                elif field is not None:
                    parts.append(fields.get(field, "?"))
        except ValueError:
            return None
        return "".join(parts)


def _query_argument(call: ast.Call) -> ast.expr | None:
    if call.args:
        return call.args[0]
    return next((kw.value for kw in call.keywords if kw.arg in {"query", "statement", "sql"}), None)


def _sql_statement(
    node: ast.expr, bindings: _Bindings, expressions: _SQLExpressions
) -> tuple[str, int] | None:
    if isinstance(node, ast.Call) and _call_name(node.func) in _SQL_EXECUTION:
        arg = _query_argument(node)
        if arg is None:
            return None
        value = _bound_value(arg, bindings)
        text = expressions.render(value, bindings) or _text(value, bindings)
        return (text, value.lineno) if text is not None else None
    text = expressions.render(node, bindings)
    return (text, node.lineno) if text is not None else None


def _sql_sites(tree: ast.Module, pattern: re.Pattern[str]) -> list[int]:
    if not any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "llm_usage_ledger" in node.value.lower()
        for node in ast.walk(tree)
    ):
        return []
    expressions = _SQLExpressions(tree)
    lines: set[int] = set()
    for node, bindings in _scoped_nodes(tree, {}):
        if isinstance(node, (ast.Call, ast.BinOp)) and (
            statement := _sql_statement(node, bindings, expressions)
        ):
            text, line = statement
            if pattern.search(_SQL_QUOTED.sub(_sql_token, text)):
                lines.add(line)
    return sorted(lines)


def ledger_inserts(tree: ast.Module, _roots: tuple[str, ...]) -> list[int]:
    return _sql_sites(tree, _LEDGER_INSERT)


def ledger_updates(tree: ast.Module, _roots: tuple[str, ...]) -> list[int]:
    return _sql_sites(tree, _LEDGER_REPRICE)


def ledger_writer_access(tree: ast.Module, _roots: tuple[str, ...]) -> list[int]:
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "write_usage_row"
    )


_MIRROR_NAMES = frozenset({"bootstrap-mirror.json", "bootstrap-mirror-enrolled"})


def _path_tail(node: ast.expr, bindings: _Bindings) -> str | None:
    node = _bound_value(node, bindings)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _path_tail(node.right, bindings)
    if isinstance(node, ast.Call) and node.args:
        name = _call_name(node.func)
        if name in {"Path", "PurePath", "join", "joinpath", "with_name"}:
            return _path_tail(node.args[-1], bindings)
        if name == "with_suffix" and isinstance(node.func, ast.Attribute):
            stem = _path_tail(node.func.value, bindings)
            suffix = _text(node.args[0], bindings)
            if stem and suffix == ".json":
                return PurePosixPath(stem).with_suffix(suffix).name
    text = _text(node, bindings)
    return PurePosixPath(text).name if text else None


def mirror_paths(tree: ast.Module, _roots: tuple[str, ...]) -> list[int]:
    literals = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "bootstrap-mirror" in node.value
    ]
    lines = {node.lineno for node in literals if _path_tail(node, {}) in _MIRROR_NAMES}
    if literals and not lines:
        lines.update(
            node.lineno
            for node, bindings in _scoped_nodes(tree, {})
            if isinstance(node, (ast.BinOp, ast.JoinedStr, ast.Call))
            and _path_tail(node, bindings) in _MIRROR_NAMES
        )
    return sorted(lines)
