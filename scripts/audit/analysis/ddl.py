# meiva:note
# The DDL text as tables and columns: comments out of the way, `CREATE TABLE` bodies split at the
# top level, `ALTER TABLE ... ADD COLUMN` folded in. Text in, columns out.
from __future__ import annotations

import re

__all__ = ["schema_columns", "strip_comments"]

_CONSTRAINT = frozenset({"CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "EXCLUDE", "LIKE"})
_ADD_COLUMN = re.compile(
    r"ALTER\s+TABLE\s+(?:ONLY\s+)?(?P<table>[\w.\"]+)\s+ADD\s+COLUMN\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?(?P<column>[\w\"]+)",
    re.IGNORECASE,
)


def strip_comments(sql: str) -> str:
    # meiva:note
    # The SQL without `--` and `/* */` comments, leaving string literals intact.
    out: list[str] = []
    i = 0
    in_str = False
    while i < len(sql):
        if not in_str and sql.startswith("--", i):
            end = sql.find("\n", i)
            i = len(sql) if end < 0 else end
            continue
        if not in_str and sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = len(sql) if end < 0 else end + 2
            continue
        if sql[i] == "'":
            in_str = not in_str
        out.append(sql[i])
        i += 1
    return "".join(out)


def _split_top_level(body: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    in_str = False
    current: list[str] = []
    for char in body:
        if char == "'":
            in_str = not in_str
        if not in_str:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif char == "," and depth == 0:
                parts.append("".join(current))
                current = []
                continue
        current.append(char)
    parts.append("".join(current))
    return parts


# meiva:link scripts/audit/docs/schema-drift.meiva.okf.md
def schema_columns(sql: str) -> dict[str, list[str]]:
    # meiva:note
    # Every table the DDL creates (and every column it adds), with its columns in order.
    text = strip_comments(sql)
    tables: dict[str, list[str]] = {}
    for match in re.finditer(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>[\w\"]+)\s*\(",
        text,
        re.IGNORECASE,
    ):
        name = match.group("name").strip('"')
        start = match.end()
        depth = 1
        in_str = False
        index = start
        while index < len(text) and depth > 0:
            char = text[index]
            if char == "'":
                in_str = not in_str
            elif not in_str:
                if char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
            index += 1
        body = text[start : index - 1]
        columns: list[str] = []
        for raw in _split_top_level(body):
            part = raw.strip()
            if not part:
                continue
            token = part.split()[0].strip('"')
            if token.upper() in _CONSTRAINT or "(" in token:
                continue
            columns.append(token)
        tables[name] = columns
    for match in _ADD_COLUMN.finditer(text):
        table = match.group("table").strip('"')
        column = match.group("column").strip('"')
        tables.setdefault(table, [])
        if column not in tables[table]:
            tables[table].append(column)
    return tables
