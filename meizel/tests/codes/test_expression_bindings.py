from __future__ import annotations

from pathlib import Path

import pytest

from meizel.graph.model import Code, FactKind, Sources
from meizel.graph.universe import Universe
from meizel.language.scan import FileScan, scan_source


def scan(tmp_path: Path, source: str) -> FileScan:
    path = "app/mod.py"
    library = tmp_path / "lib" / "api.py"
    library.parent.mkdir(exist_ok=True)
    library.write_text("VALUE = 1\n", encoding="utf-8")
    universe = Universe(tmp_path, [path, "lib/__init__.py", "lib/api.py"])
    return scan_source(universe, path, source.encode(), Sources())


@pytest.mark.parametrize(
    "body",
    [
        "lookup = lambda os: os.getenv('KEY')\n",
        "values = [os.getenv('KEY') for os in providers]\n",
        "def lookup(provider):\n"
        "    match provider:\n"
        "        case os:\n"
        "            return os.getenv('KEY')\n",
    ],
    ids=["lambda-parameter", "comprehension-target", "match-capture"],
)
def test_local_bindings_are_not_process_environment_reads(tmp_path: Path, body: str) -> None:
    assert scan(tmp_path, "import os\n" + body).ambient == []


@pytest.mark.parametrize("shadow", [True, False], ids=["local", "imported"])
@pytest.mark.parametrize("syntax", ["lambda", "comprehension", "match"])
@pytest.mark.parametrize(
    ("consumer", "imported", "name", "expression"),
    [
        ("ambient", "import os", "os", "os.getenv('KEY')"),
        ("purity", "import time", "time", "time.time()"),
        ("import", "from lib import api", "api", "api.VALUE"),
        ("patch", "import time", "time", "setattr(time, 'sleep', None)"),
    ],
)
def test_expression_bindings_reach_the_existing_consumers(
    tmp_path: Path,
    consumer: str,
    imported: str,
    name: str,
    expression: str,
    syntax: str,
    shadow: bool,
) -> None:
    binding = name if shadow else "other"
    bodies = {
        "lambda": f"value = lambda {binding}: {expression}\n",
        "comprehension": f"values = [{expression} for {binding} in providers]\n",
        "match": f"def value(provider):\n    match provider:\n        case {binding}:\n            return {expression}\n",
    }
    result = scan(tmp_path, imported + "\n" + bodies[syntax])
    observed = {
        "ambient": bool(result.ambient),
        "purity": any(item.code is Code.IMPURE_CALL for item in result.purity),
        "import": any(fact.kind is FactKind.ATTRIBUTE for fact in result.facts),
        "patch": bool(result.patches),
    }
    assert observed[consumer] is not shadow


@pytest.mark.parametrize("parameters", ["os=", "*, os="])
def test_lambda_defaults_use_the_enclosing_scope(tmp_path: Path, parameters: str) -> None:
    result = scan(
        tmp_path,
        "import os\nlookup = lambda " + parameters + "os.getenv('DEFAULT'): os.getenv('LOCAL')\n",
    )
    assert [(item.kind.value, item.target) for item in result.ambient] == [("environ", "os.getenv")]


@pytest.mark.parametrize(
    "expression",
    [
        "[os.getenv('BODY') for os in os.getenv('OUTER')]",
        "{os.getenv('BODY') for os in os.getenv('OUTER')}",
        "{key: os.getenv('BODY') for key, os in os.getenv('OUTER')}",
        "(os.getenv('BODY') for os in os.getenv('OUTER'))",
    ],
)
def test_only_the_first_comprehension_iter_uses_the_outer_scope(
    tmp_path: Path, expression: str
) -> None:
    result = scan(tmp_path, "import os\nvalues = " + expression + "\n")
    assert [item.target for item in result.ambient] == ["os.getenv"]


def test_later_generators_and_filters_share_the_comprehension_scope(tmp_path: Path) -> None:
    result = scan(
        tmp_path,
        "import os\nvalues = [item for os in providers "
        "for item in os.getenv('ITER') if os.getenv('FILTER')]\n",
    )
    assert result.ambient == []


@pytest.mark.parametrize("pattern", ["[os, *rest]", "[*os]", "{'key': item, **os}"])
def test_match_pattern_names_shadow_imports(tmp_path: Path, pattern: str) -> None:
    source = (
        "import os\ndef lookup(provider):\n    match provider:\n"
        f"        case {pattern}:\n            return os.getenv('KEY')\n"
    )
    assert scan(tmp_path, source).ambient == []


@pytest.mark.parametrize(
    "body",
    [
        "lookup = lambda: os.getenv('GLOBAL')",
        "values = [os.getenv('GLOBAL') for item in os.getenv('CLASS')]",
        "class Nested:\n        lookup = lambda: os.getenv('GLOBAL')",
        "def lookup(self):\n        return os.getenv('GLOBAL')",
    ],
)
def test_closures_skip_class_namespaces(tmp_path: Path, body: str) -> None:
    result = scan(tmp_path, "import os\nclass Owner:\n    os = provider\n    " + body + "\n")
    assert [item.target for item in result.ambient] == ["os.getenv"]


def test_a_comprehension_does_not_bind_its_target_in_the_outer_scope(tmp_path: Path) -> None:
    result = scan(
        tmp_path,
        "import os\nvalues = [os.getenv('LOCAL') for os in providers]\n"
        "value = os.getenv('GLOBAL')\n",
    )
    assert [item.target for item in result.ambient] == ["os.getenv"]


@pytest.mark.parametrize(
    "body",
    [
        "lookup = lambda api=api.VALUE: api.VALUE",
        "values = [api.VALUE for api in api.VALUE]",
    ],
)
def test_default_and_first_iter_preserve_real_import_attribute_facts(
    tmp_path: Path, body: str
) -> None:
    result = scan(tmp_path, "from lib import api\n" + body + "\n")
    assert [(fact.dst, fact.names) for fact in result.facts if fact.kind is FactKind.ATTRIBUTE] == [
        ("lib/api.py", ("VALUE",))
    ]


@pytest.mark.parametrize(
    "body",
    [
        "values = [os.getenv('LOCAL') for *os, last in providers]",
        "async def lookup(providers):\n    return [os.getenv('LOCAL') async for os in providers]",
        "lookup = lambda: ((os := provider), os.getenv('LOCAL'))",
        "lookup = lambda os: (lambda: os.getenv('LOCAL'))",
    ],
)
def test_expression_scope_variants_keep_their_local_bindings(tmp_path: Path, body: str) -> None:
    assert scan(tmp_path, "import os\n" + body + "\n").ambient == []


def test_a_subscript_target_retains_the_sys_modules_guard(tmp_path: Path) -> None:
    result = scan(tmp_path, "import sys\nvalues = [None for sys.modules['lib.api'] in providers]\n")
    assert any(
        item.form == "sys.modules write of a first-party module" for item in result.restricted
    )


def test_an_attribute_target_retains_the_import_attribute_fact(tmp_path: Path) -> None:
    result = scan(tmp_path, "from lib import api\nvalues = [None for api.VALUE in providers]\n")
    assert [(fact.dst, fact.names) for fact in result.facts if fact.kind is FactKind.ATTRIBUTE] == [
        ("lib/api.py", ("VALUE",))
    ]


def test_a_call_in_a_subscript_target_retains_its_ambient_acquisition(tmp_path: Path) -> None:
    result = scan(
        tmp_path, "from os import getenv\nvalues = [None for buckets[getenv('KEY')] in providers]\n"
    )
    assert [item.target for item in result.ambient] == ["os.getenv"]


@pytest.mark.parametrize("parameter", ["value=", "*, value="])
def test_lambda_default_walrus_binds_in_the_enclosing_scope(tmp_path: Path, parameter: str) -> None:
    result = scan(
        tmp_path,
        "import os\nlookup = lambda "
        + parameter
        + "(os := provider): None\nvalue = os.getenv('KEY')\n",
    )
    assert result.ambient == []


@pytest.mark.parametrize(
    "expression",
    [
        "lambda: (os := provider)",
        "lambda value=(lambda: (os := provider)): None",
        "lambda: (lambda value=(os := provider): None)",
    ],
)
def test_lambda_bodies_do_not_leak_walrus_bindings_to_the_module(
    tmp_path: Path, expression: str
) -> None:
    result = scan(tmp_path, "import os\nlookup = " + expression + "\nvalue = os.getenv('GLOBAL')\n")
    assert [item.target for item in result.ambient] == ["os.getenv"]


@pytest.mark.parametrize(
    "header",
    [
        "def lookup(value=BOUND):",
        "def lookup(*, value=BOUND):",
        "async def lookup(value=BOUND):",
        "@BOUND\ndef lookup():",
        "def lookup(value: BOUND):",
        "def lookup(value: BOUND, /):",
        "def lookup(*, value: BOUND):",
        "def lookup(*values: BOUND):",
        "def lookup(**values: BOUND):",
        "def lookup() -> BOUND:",
        "class Owner(BOUND):",
        "class Owner(metaclass=BOUND):",
        "@BOUND\nclass Owner:",
    ],
)
def test_definition_headers_bind_in_the_enclosing_scope(tmp_path: Path, header: str) -> None:
    source = "import os\n" + header.replace("BOUND", "(os := provider)")
    source += "\n    pass\nvalue = os.getenv('KEY')\n"
    assert scan(tmp_path, source).ambient == []


@pytest.mark.parametrize(
    "header",
    [
        "def lookup(value=BOUND):",
        "def lookup(*, value=BOUND):",
        "async def lookup(value=BOUND):",
        "@BOUND\ndef lookup():",
        "def lookup(value: BOUND):",
        "def lookup() -> BOUND:",
        "class Owner(BOUND):",
        "class Owner(metaclass=BOUND):",
        "@BOUND\nclass Owner:",
    ],
)
def test_definition_headers_preserve_real_import_facts(tmp_path: Path, header: str) -> None:
    source = "from lib import api\n" + header.replace("BOUND", "api.VALUE") + "\n    pass\n"
    result = scan(tmp_path, source)
    assert [(fact.dst, fact.names) for fact in result.facts if fact.kind is FactKind.ATTRIBUTE] == [
        ("lib/api.py", ("VALUE",))
    ]


@pytest.mark.parametrize("shadow", [True, False], ids=["local", "imported"])
@pytest.mark.parametrize(
    "header",
    ["def lookup(value=BOUND):", "@BOUND\ndef lookup():", "class Owner(BOUND):"],
)
@pytest.mark.parametrize(
    ("consumer", "imported", "name", "expression"),
    [
        ("ambient", "import os", "os", "os.getenv('KEY')"),
        ("purity", "import time", "time", "time.time()"),
        ("import", "from lib import api", "api", "api.VALUE"),
        ("patch", "import time", "time", "setattr(time, 'sleep', None)"),
    ],
)
def test_definition_header_bindings_reach_existing_consumers(
    tmp_path: Path,
    consumer: str,
    imported: str,
    name: str,
    expression: str,
    header: str,
    shadow: bool,
) -> None:
    binding = name if shadow else "other"
    source = imported + "\n" + header.replace("BOUND", f"({binding} := provider)")
    source += "\n    pass\nvalue = " + expression + "\n"
    result = scan(tmp_path, source)
    observed = {
        "ambient": bool(result.ambient),
        "purity": any(item.code is Code.IMPURE_CALL for item in result.purity),
        "import": any(fact.kind is FactKind.ATTRIBUTE for fact in result.facts),
        "patch": bool(result.patches),
    }
    assert observed[consumer] is not shadow


@pytest.mark.parametrize(
    "body",
    [
        "def lookup():\n    os = provider",
        "async def lookup():\n    os = provider",
        "class Owner:\n    os = provider",
        "def lookup(value=(lambda: (os := provider))):\n    pass",
        "class Owner((lambda: (os := provider))):\n    pass",
    ],
)
def test_definition_bodies_do_not_bind_names_in_the_outer_scope(tmp_path: Path, body: str) -> None:
    source = "import os\n" + body + "\nvalue = os.getenv('KEY')\n"
    assert [item.target for item in scan(tmp_path, source).ambient] == ["os.getenv"]


@pytest.mark.parametrize("header", ["def lookup[os]():", "class Owner[os]:"])
def test_type_parameters_do_not_leak_into_the_outer_scope(tmp_path: Path, header: str) -> None:
    source = "import os\n" + header + "\n    pass\nvalue = os.getenv('KEY')\n"
    assert [item.target for item in scan(tmp_path, source).ambient] == ["os.getenv"]


@pytest.mark.parametrize("header", ["def lookup[T](value=BOUND):", "@BOUND\nclass Owner[T]:"])
def test_generic_definition_outer_headers_keep_their_bindings(tmp_path: Path, header: str) -> None:
    source = "import os\n" + header.replace("BOUND", "(os := provider)")
    source += "\n    pass\nvalue = os.getenv('KEY')\n"
    assert scan(tmp_path, source).ambient == []
