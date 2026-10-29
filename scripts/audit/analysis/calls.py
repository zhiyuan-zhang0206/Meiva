# meiva:note
# The call-site judge behind `call_sites.py`: which `[module:]function:parameter[:position]` spec
# means what, and whether one call's argument list can carry that parameter.
#
# A required parameter added to a shared callable breaks every caller that does not pass it, and the
# break hides in three places: a scoped type-check never sees the callers outside the trees it was
# given (`scripts/tests`, `tests/e2e`, a plugin's own tree); a call that hands the value over
# positionally never appears in a search for `parameter=`; and a caller written inside a string
# literal — a `pytester` fixture that writes a test module and runs it — is not a call node at all,
# so an AST walk passes it. This reads the calls, including the source a test writes into a string,
# instead of the text.
#
# `judge_calls` takes the texts the shell read and returns one finding per call that cannot supply
# the parameter, plus how many calls it judged at all: a count of zero under a spec that names a real
# function means the `module` narrowing hid the call sites, which is the one reading that separates
# "nothing to fix" from "the search did not happen". The walk, the exit code and printing are the
# shell's; the verdicts and what they mean are here.

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

# meiva:note
# The four answers one call can give. `keyword` and `position` name the ways the parameter arrives;
# `unproven` is an argument list or a call form this judge cannot read, which proves nothing either
# way and is never a failure.
KEYWORD = "keyword"
POSITION = "position"
UNPROVEN = "unproven"
MISSING = "missing"

# meiva:note
# Where a finding was found, when the call was not ordinary module-level source.
EMBEDDED = "inside a string literal"
NAMED_BY_STRING = "named by a string"

# meiva:note
# The calls that reach a function by name, so the parameter can never be seen at the call site. A
# mention of the name in one of these is recorded as unproven rather than passed over.
_STRING_NAME_CALLS = frozenset({"getattr", "setattr", "patch", "delattr"})

# meiva:note
# What a string literal has to contain before it is read as a module a test writes and runs: a
# definition or an import at the start of a line. A sentence that mentions a call has neither, which
# is what keeps documentation and error messages out of the report.
_CODE_MARKERS = ("\ndef ", "\nclass ", "\nasync def ", "\nimport ", "\nfrom ", "\n@pytest")
_CODE_PREFIXES = ("def ", "class ", "async def ", "import ", "from ", "pytest_plugins")


# meiva:note
# One callable parameter to look for, and how a call may carry it. `module` narrows the search to
# files that can reach it; `position` (0-based, in the call's own positional arguments) says a call
# may satisfy the parameter positionally. The position is the only place the judge needs the
# signature, and it takes it from the operator rather than importing the function: the tool has to
# run before the tree imports cleanly with the new signature.
@dataclass(frozen=True)
class CallSpec:
    function: str
    parameter: str
    module: str | None = None
    position: int | None = None

    def render(self) -> str:
        head = f"{self.module}:" if self.module else ""
        return f"{head}{self.function}:{self.parameter}"


# meiva:note
# One judged call: where it is, how it stands to the parameter, and whether it was ordinary source or
# a form this judge had to read another way.
@dataclass(frozen=True)
class CallSiteFinding:
    path: str
    line: int
    spec: CallSpec
    verdict: str
    where: str = ""


# meiva:note
# `[module:]function:parameter[:position]`, with the module and position optional. A spec that does
# not parse is an error, never a silently empty search.
def parse_call_spec(text: str) -> CallSpec:
    parts = text.split(":")
    if len(parts) == 2:
        function, parameter = parts
        module, position = None, None
    elif len(parts) == 3:
        if parts[2].isdigit():
            function, parameter, position_text = parts
            module, position = None, int(position_text)
        else:
            module, function, parameter = parts
            position = None
    elif len(parts) == 4:
        module, function, parameter, position_text = parts
        if not position_text.isdigit():
            raise ValueError(f"spec {text!r}: the position must be a number")
        position = int(position_text)
    else:
        raise ValueError(f"spec {text!r}: expected [module:]function:parameter[:position]")
    if not function or not parameter:
        raise ValueError(f"spec {text!r}: the function and the parameter are both required")
    return CallSpec(function=function, parameter=parameter, module=module, position=position)


# meiva:note
# The name a call spells: a bare `build(...)` or an attribute `emitter.build(...)`. Anything else (a
# subscript, a call result being called) has no name this judge can follow.
def _called_name(call: ast.Call) -> str:
    callee = call.func
    if isinstance(callee, ast.Name):
        return callee.id
    if isinstance(callee, ast.Attribute):
        return callee.attr
    return ""


# meiva:note
# Whether the source can reach `module`: it defines a top-level name the module path ends with, or it
# imports the module or one of its members. This is the only narrowing the judge does, and a spec
# without a module skips it.
def _mentions(tree: ast.Module, module: str) -> bool:
    last = module.rsplit(".", 1)[-1]
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and (node.module == module or node.module.endswith(f".{last}"))
        ):
            return True
        if isinstance(node, ast.Import) and any(
            alias.name == module or alias.name.startswith(f"{module}.") for alias in node.names
        ):
            return True
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and (
            node.name == last
        ):
            return True
    return False


# meiva:note
# How one call stands to one spec: a keyword argument names the parameter; an explicit positional
# index says the call hands it over positionally; an expanded argument list proves nothing; anything
# else cannot supply it.
def _verdict(call: ast.Call, spec: CallSpec) -> str:
    for keyword in call.keywords:
        if keyword.arg == spec.parameter:
            return KEYWORD
        if keyword.arg is None:
            return UNPROVEN
    if any(isinstance(argument, ast.Starred) for argument in call.args):
        return UNPROVEN
    if spec.position is not None and len(call.args) > spec.position:
        return POSITION
    return MISSING


# meiva:note
# A parse that fails is not an error here: a generated or half-written source has no call site to
# judge, and refusing the whole run over one file helps nobody.
def _parse(text: str) -> ast.Module | None:
    try:
        return ast.parse(text)
    except SyntaxError:
        return None


# meiva:note
# Whether a string literal is a module a test writes out and runs rather than prose. The marker has
# to start a line, so `see init_telemetry()` in a sentence stays out of the report.
def _looks_like_code(text: str) -> bool:
    if "\n" not in text:
        return False
    if text.startswith(_CODE_PREFIXES):
        return True
    padded = "\n" + text
    return any(marker in padded for marker in _CODE_MARKERS)


# meiva:note
# Every module written into a string in this source, as (the line the literal starts on, its text).
def _embedded_sources(tree: ast.Module) -> Iterator[tuple[int, str]]:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _looks_like_code(node.value)
        ):
            yield node.lineno, node.value


# meiva:note
# The units one file is judged as: the file itself, then each module it writes into a string. The
# offset is what turns a line inside that module back into a line of the file a reader can open.
def _units(text: str) -> Iterator[tuple[int, str, ast.Module | None, str]]:
    tree = _parse(text)
    yield 0, text, tree, ""
    if tree is None:
        return
    for lineno, inner in _embedded_sources(tree):
        yield lineno - 1, inner, _parse(inner), EMBEDDED


# meiva:note
# The calls whose name arrives as a string (`getattr(module, "build")`): the argument list at the
# call site says nothing about the parameter, so each mention is recorded as unproven.
def _named_by_string(tree: ast.Module, spec: CallSpec) -> Iterator[int]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _called_name(node) not in _STRING_NAME_CALLS:
            continue
        if any(
            isinstance(argument, ast.Constant) and argument.value == spec.function
            for argument in node.args
        ):
            yield node.lineno


# meiva:note
# Every call in the given sources that cannot supply the parameter, in source order, plus the number
# of calls judged.
def judge_calls(
    files: Iterable[tuple[str, str]], specs: list[CallSpec]
) -> tuple[list[CallSiteFinding], int]:
    findings: list[CallSiteFinding] = []
    judged = 0
    for path, text in files:
        for offset, unit_text, tree, where in _units(text):
            if tree is None:
                findings.extend(_unreadable(path, offset, unit_text, specs, where))
                continue
            for spec in specs:
                if spec.module is not None and not _mentions(tree, spec.module):
                    continue
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call) or _called_name(node) != spec.function:
                        continue
                    judged += 1
                    found = _verdict(node, spec)
                    if found in (MISSING, UNPROVEN):
                        findings.append(
                            CallSiteFinding(path, offset + node.lineno, spec, found, where)
                        )
        findings.extend(_string_named(path, text, specs))
    findings.sort(key=lambda finding: (finding.path, finding.line, finding.spec.render()))
    return findings, judged


# meiva:note
# A string that looks like a module but does not parse still names the call it was meant to make, so
# it is reported as unproven at the line the literal starts on. Silence here is what let a broken
# `pytester` fixture pass as "no call sites".
def _unreadable(
    path: str, offset: int, text: str, specs: list[CallSpec], where: str
) -> list[CallSiteFinding]:
    if not where:
        return []
    return [
        CallSiteFinding(path, offset + 1, spec, UNPROVEN, where)
        for spec in specs
        if f"{spec.function}(" in text
    ]


# meiva:note
# The string-named calls of one file, narrowed the same way the ordinary walk is.
def _string_named(path: str, text: str, specs: list[CallSpec]) -> list[CallSiteFinding]:
    tree = _parse(text)
    if tree is None:
        return []
    return [
        CallSiteFinding(path, line, spec, UNPROVEN, NAMED_BY_STRING)
        for spec in specs
        if spec.module is None or _mentions(tree, spec.module)
        for line in _named_by_string(tree, spec)
    ]


# meiva:note
# One line per finding plus the counts, which are what tells a reader whether the search found the
# call sites at all. Presentation only: the caller decides what an exit code means.
def render_call_sites(findings: list[CallSiteFinding], judged: int) -> str:
    lines = [
        f"{finding.path}:{finding.line}: {finding.spec.function}() "
        f"{'cannot supply' if finding.verdict == MISSING else 'cannot prove'} "
        f"required {finding.spec.parameter!r} [{finding.spec.render()}]"
        + (f" ({finding.where})" if finding.where else "")
        for finding in findings
    ]
    missing = sum(1 for finding in findings if finding.verdict == MISSING)
    unproven = len(findings) - missing
    lines.append(f"call sites checked: {judged}; missing: {missing}; unproven: {unproven}")
    return "\n".join(lines)


# meiva:note
# The same report for a script that counts rather than reads.
def call_site_payload(findings: list[CallSiteFinding], judged: int) -> dict[str, object]:
    return {
        "checked": judged,
        "findings": [
            {
                "path": finding.path,
                "line": finding.line,
                "function": finding.spec.function,
                "parameter": finding.spec.parameter,
                "verdict": finding.verdict,
                "where": finding.where,
            }
            for finding in findings
        ],
    }
