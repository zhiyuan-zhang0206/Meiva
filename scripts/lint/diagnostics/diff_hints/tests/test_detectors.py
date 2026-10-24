from __future__ import annotations

import pytest

from scripts.lint.diagnostics.diff_hints.model import FileChange, HintId, RemovedLine
from scripts.lint.diagnostics.diff_hints.removed import deleted_tests
from scripts.lint.diagnostics.diff_hints.tests.support import ROLES, hints, sites

# meiva:note
# Each case: the hint id, the path of the file, a source that must raise the hint and a
# source that must not. Production hints look at application packages, test hints at
# tests, and a composition root is found by its file name.
CASES = [
    pytest.param(
        HintId.AMBIENT_READ,
        "cli/x.py",
        "import time\n\ndef f():\n    return time.time()\n",
        "import time\n\ndef f(now: float):\n    time.sleep(1)\n    return now\n",
        id="ambient-clock",
    ),
    pytest.param(
        HintId.AMBIENT_READ,
        "cli/x.py",
        "from datetime import UTC, datetime\n\ndef f():\n    return datetime.now(UTC)\n",
        "from datetime import datetime\n\ndef f(now: datetime):\n    return now\n",
        id="ambient-datetime",
    ),
    pytest.param(
        HintId.AMBIENT_READ,
        "cli/x.py",
        "import os\nfrom pathlib import Path\n\ndef f():\n    return os.environ, Path.home()\n",
        "from pathlib import Path\n\ndef f(home: Path):\n    return home / 'x'\n",
        id="ambient-environment",
    ),
    pytest.param(
        HintId.AMBIENT_READ,
        "cli/x.py",
        "from base.config import settings\n\ndef f():\n    return settings.agent\n",
        "from base.config import settings\n\ndef f(config: object):\n    return config\n",
        id="ambient-settings",
    ),
    pytest.param(
        HintId.CALLABLE_DEFAULT,
        "cli/x.py",
        "def f(now_fn=None):\n    return now_fn\n",
        "def f(now_fn):\n    return now_fn\n",
        id="callable-default-by-name",
    ),
    pytest.param(
        HintId.CALLABLE_DEFAULT,
        "cli/x.py",
        "from collections.abc import Callable\n\ndef f(run: Callable[[], int] = int):\n    return run\n",
        "def f(timeout: float = 3.0):\n    return timeout\n",
        id="callable-default-by-type",
    ),
    pytest.param(
        HintId.CALLABLE_BAG,
        "cli/x.py",
        "from collections.abc import Callable\nfrom dataclasses import dataclass\n\n@dataclass(frozen=True)\nclass Deps:\n    a: Callable[[], int]\n    b: Callable[[], int]\n",
        "from collections.abc import Callable\nfrom dataclasses import dataclass\n\n@dataclass(frozen=True)\nclass Step:\n    apply: Callable[[], int]\n    name: str\n",
        id="callable-bag",
    ),
    pytest.param(
        HintId.NONE_FALLBACK,
        "cli/x.py",
        "def f(client=None):\n    client = client or make_client()\n    return client\n",
        "def f(items=None):\n    items = items or []\n    return items\n",
        id="none-fallback-or",
    ),
    pytest.param(
        HintId.NONE_FALLBACK,
        "cli/x.py",
        "def f(client=None):\n    if client is None:\n        client = Client()\n    return client\n",
        "def f(client):\n    return client\n",
        id="none-fallback-if",
    ),
    pytest.param(
        HintId.NONE_FALLBACK,
        "cli/x.py",
        "def f(conn=None):\n    return pool.get() if conn is None else conn\n",
        "def f(conn=None):\n    return conn if conn is not None else []\n",
        id="none-fallback-conditional",
    ),
    pytest.param(
        HintId.WILDCARD_CASE,
        "cli/x.py",
        "def f(x):\n    match x:\n        case 1:\n            return 1\n        case _:\n            pass\n",
        "from typing import assert_never\n\ndef f(x: int):\n    match x:\n        case 1:\n            return 1\n        case _:\n            assert_never(x)\n",
        id="wildcard-case",
    ),
    pytest.param(
        HintId.WILDCARD_CASE,
        "cli/x.py",
        "def f(action: str):\n    if action == 'a':\n        return 1\n    return 2\n",
        "def f(action: str):\n    if action == 'a':\n        return 1\n    raise ValueError(action)\n",
        id="wildcard-string-selector",
    ),
    pytest.param(
        HintId.ANY_COLLABORATOR,
        "cli/x.py",
        "from typing import Any\n\ndef f(pool: Any):\n    return pool\n",
        "from typing import Any\n\ndef f(payload: dict[str, Any], value: object):\n    return payload, value\n",
        id="any-collaborator",
    ),
    pytest.param(
        HintId.ANY_COLLABORATOR,
        "cli/x.py",
        "def f(config: object):\n    return config\n",
        "class A:\n    def __eq__(self, other: object) -> bool:\n        return True\n",
        id="object-collaborator",
    ),
    pytest.param(
        HintId.SERVICE_LOCATOR,
        "gateway/x.py",
        "def f(request):\n    return request.app.state.thing\n",
        "def f(config):\n    return config.thing\n",
        id="service-locator-state",
    ),
    pytest.param(
        HintId.SERVICE_LOCATOR,
        "gateway/x.py",
        "def f(request):\n    return runtime_view(request)\n",
        "def f(request):\n    return request\n",
        id="service-locator-view",
    ),
    pytest.param(
        HintId.MUTABLE_IN_FROZEN,
        "cli/x.py",
        "from dataclasses import dataclass\n\n@dataclass(frozen=True)\nclass V:\n    items: list[str]\n",
        "from dataclasses import dataclass\n\n@dataclass(frozen=True)\nclass V:\n    items: tuple[str, ...]\n",
        id="mutable-in-frozen",
    ),
    pytest.param(
        HintId.MUTABLE_IN_FROZEN,
        "cli/x.py",
        "from dataclasses import dataclass\nfrom typing import Any\n\n@dataclass(frozen=True)\nclass V:\n    holder: Any\n",
        "from dataclasses import dataclass\n\n@dataclass\nclass V:\n    items: list[str]\n",
        id="mutable-in-unfrozen-is-fine",
    ),
    pytest.param(
        HintId.LAMBDA_DEPENDENCY,
        "cli/x.py",
        "def f():\n    return g(path_reader=lambda: p)\n",
        "def f():\n    return g(callback=lambda: p)\n",
        id="lambda-reader",
    ),
    pytest.param(
        HintId.LAMBDA_DEPENDENCY,
        "cli/x.py",
        "def f(clock):\n    return g(clock_factory=lambda: clock)\n",
        "def f():\n    return g(clock_factory=lambda: build())\n",
        id="lambda-factory-wrapping-a-value",
    ),
    pytest.param(
        HintId.TEST_SEAM,
        "cli/tests/test_x.py",
        "def test_a(obj):\n    vars(obj)['x'] = 1\n",
        "def test_a(obj):\n    assert obj.x == 1\n",
        id="seam-vars",
    ),
    pytest.param(
        HintId.TEST_SEAM,
        "cli/tests/test_x.py",
        "def test_a(obj):\n    object.__setattr__(obj, 'x', 1)\n",
        "def test_a(obj):\n    assert obj\n",
        id="seam-object-setattr",
    ),
    pytest.param(
        HintId.TEST_SEAM,
        "cli/tests/test_x.py",
        "def test_a(monkeypatch):\n    monkeypatch.setattr('a.b', 1, raising=False)\n",
        "def test_a(monkeypatch):\n    monkeypatch.setattr('a.b', 1)\n",
        id="seam-raising-false",
    ),
    pytest.param(
        HintId.TEST_SEAM,
        "cli/tests/test_x.py",
        "import sys\n\ndef test_a():\n    sys.modules['m'] = object()\n",
        "import sys\n\ndef test_a():\n    assert 'm' not in sys.modules\n",
        id="seam-sys-modules",
    ),
    pytest.param(
        HintId.TEST_SEAM,
        "cli/tests/test_x.py",
        "def test_a(mock):\n    mock.run.side_effect = OSError()\n",
        "def test_a(mock):\n    mock.run()\n",
        id="seam-side-effect",
    ),
    pytest.param(
        HintId.TEST_SEAM,
        "cli/tests/test_x.py",
        "import httpx\n\ndef test_a():\n    httpx.MockTransport(lambda r: None)\n",
        "import httpx\n\ndef test_a():\n    httpx.Client()\n",
        id="seam-mock-transport",
    ),
    pytest.param(
        HintId.TEST_SEAM,
        "cli/tests/test_x.py",
        "import time\n\ndef test_a():\n    time.sleep(1)\n",
        "def test_a():\n    assert True\n",
        id="seam-sleep-in-a-test-module",
    ),
    pytest.param(
        HintId.TEST_SEAM,
        "cli/tests/test_x.py",
        "import os\n\ndef test_a():\n    os.environ['X'] = '1'\n",
        "import os\n\ndef test_a():\n    assert os.sep\n",
        id="seam-environment-write",
    ),
    pytest.param(
        HintId.TEST_SEAM,
        "cli/tests/test_x.py",
        "import inspect\nimport cli.x\n\ndef test_a():\n    assert 'x' in inspect.getsource(cli.x)\n",
        "import cli.x\n\ndef test_a():\n    assert cli.x\n",
        id="seam-source-text",
    ),
    pytest.param(
        HintId.TEST_SEAM,
        "cli/tests/test_x.py",
        "import cli.x\n\ndef test_a():\n    cli.x.value = 3\n",
        "def test_a():\n    local = object()\n    assert local\n",
        id="seam-imported-attribute",
    ),
    pytest.param(
        HintId.TEST_SEAM,
        "cli/tests/test_x.py",
        "def test_a(runtime):\n    runtime.serve = lambda: None\n",
        "def test_a(runtime):\n    assert runtime\n",
        id="seam-replaced-method",
    ),
    pytest.param(
        HintId.TEST_SUBCLASS,
        "cli/tests/test_x.py",
        "from base.config import ConfigBoot\n\nclass Fake(ConfigBoot):\n    def get_field(self, name):\n        return 1\n",
        "from base.config import ConfigBoot\n\nclass Fake(ConfigBoot):\n    pass\n",
        id="subclass-overriding-a-method",
    ),
    pytest.param(
        HintId.TEST_SUBCLASS,
        "cli/tests/test_x.py",
        "from psycopg_pool import AsyncConnectionPool\n\nclass Pool(AsyncConnectionPool):\n    def connection(self):\n        return 1\n",
        "class MyError(Exception):\n    def hint(self):\n        return 1\n",
        id="subclass-of-a-resource-client",
    ),
    pytest.param(
        HintId.SPY_ASSERT,
        "cli/tests/test_x.py",
        "def test_a(m):\n    m.assert_called_once_with(1)\n",
        "def test_a(m):\n    assert m == 1\n",
        id="spy-assert-called",
    ),
    pytest.param(
        HintId.SPY_ASSERT,
        "cli/tests/test_x.py",
        "def test_a(m):\n    assert m.call_args == 1\n",
        "def test_a(m):\n    assert m.args == 1\n",
        id="spy-assert-call-args",
    ),
]


@pytest.mark.parametrize(("hint", "path", "positive", "negative"), CASES)
def test_a_hint_fires_on_its_shape_and_not_on_the_clean_shape(
    hint: HintId, path: str, positive: str, negative: str
) -> None:
    assert hint in hints(positive, path)
    assert hint not in hints(negative, path)


def test_a_composition_root_is_found_by_its_file_name() -> None:
    clock = "import time\n\ndef f():\n    return time.time()\n"
    locator = "def f(request):\n    return request.app.state.thing\n"
    assert hints(clock, "cli/main.py") == []
    assert hints(clock, "cli/commands/runtime.py") == []
    assert hints(locator, "gateway/app.py") == []
    assert hints(clock, "cli/other.py") == [HintId.AMBIENT_READ]


def test_a_sleep_in_a_helper_script_is_the_real_resource_not_a_seam() -> None:
    sleeper = "import time\n\ndef sleeper():\n    time.sleep(1)\n"
    waiting = "import time\n\ndef test_a():\n    time.sleep(1)\n"
    assert hints(sleeper, "cli/tests/sleep_child.py") == []
    assert hints(waiting, "cli/tests/test_x.py") == [HintId.TEST_SEAM]


def test_a_protocol_fires_above_base_and_not_in_base() -> None:
    source = (
        "from typing import Protocol\n\nclass Port(Protocol):\n    def run(self) -> None: ...\n"
    )
    assert hints(source, "cli/x.py") == [HintId.PROTOCOL_OUTSIDE_BASE]
    assert hints(source, "base/x.py") == []
    assert hints("from typing import Protocol\n\nclass P(Protocol[int]): ...\n", "cli/x.py") == [
        HintId.PROTOCOL_OUTSIDE_BASE
    ]


def test_production_hints_do_not_judge_tests_and_test_hints_do_not_judge_production() -> None:
    clock = "import time\n\ndef f():\n    return time.time()\n"
    assert hints(clock, "cli/tests/test_x.py") == []
    spy = "def f(m):\n    m.assert_called_once()\n"
    assert hints(spy, "cli/x.py") == []


def test_files_outside_the_application_and_test_packages_are_not_judged() -> None:
    clock = "import time\n\ndef f():\n    return time.time()\n"
    assert hints(clock, "scripts/x.py") == []
    assert hints(clock, "docs/x.txt") == []


def test_only_added_lines_raise_a_hint() -> None:
    source = "import time\n\ndef f():\n    return time.time()\n\ndef g():\n    return time.time()\n"
    assert [s.line for s in sites(source)] == [4, 7]
    assert [s.line for s in sites(source, added=frozenset({7}))] == [7]
    assert sites(source, added=frozenset({1, 2})) == []


def test_a_file_that_does_not_parse_raises_nothing() -> None:
    assert sites("def broken(:\n") == []


def test_a_wildcard_case_that_raises_is_fine() -> None:
    source = "def f(x):\n    match x:\n        case 1:\n            return 1\n        case _:\n            raise ValueError(x)\n"
    assert hints(source) == []


def _removal(path: str, *names: str) -> FileChange:
    removed = tuple(RemovedLine(index + 10, f"def {name}():") for index, name in enumerate(names))
    return FileChange(path, frozenset(), (), removed, deleted=False)


def test_removed_tests_are_listed_per_file() -> None:
    found = deleted_tests((_removal("cli/tests/test_x.py", "test_a", "test_b"),), ROLES)
    assert [(s.hint, s.path, s.line) for s in found] == [
        (HintId.DELETED_TEST, "cli/tests/test_x.py", 10)
    ]
    assert "2 test(s) removed: test_a, test_b" in found[0].detail


def test_a_test_added_back_in_a_file_of_the_same_name_is_a_move_not_a_deletion() -> None:
    moved_to = FileChange(
        "cli/pkg/tests/test_x.py", frozenset({1}), ("def test_a():",), (), deleted=False
    )
    removal = _removal("cli/tests/test_x.py", "test_a", "test_b")
    found = deleted_tests((removal, moved_to), ROLES)
    assert "1 test(s) removed: test_b" in found[0].detail


def test_the_same_test_name_in_an_unrelated_file_is_a_different_test() -> None:
    elsewhere = FileChange(
        "cli/tests/test_other.py", frozenset({1}), ("def test_a():",), (), deleted=False
    )
    found = deleted_tests((_removal("cli/tests/test_x.py", "test_a"), elsewhere), ROLES)
    assert "1 test(s) removed: test_a" in found[0].detail


def test_removing_a_non_test_function_or_production_code_raises_nothing() -> None:
    assert deleted_tests((_removal("cli/tests/test_x.py", "helper"),), ROLES) == []
    assert deleted_tests((_removal("cli/x.py", "test_a"),), ROLES) == []


def test_a_field_compared_with_a_literal_is_left_to_review() -> None:
    assert hints("def f(fact):\n    return fact.role != 'shell'\n") == []


def test_a_sleep_that_paces_a_poll_is_the_real_resource_form() -> None:
    polling = "import time\n\ndef test_a():\n    while not ready():\n        time.sleep(0.05)\n"
    waiting = "import time\n\ndef test_a():\n    time.sleep(0.2)\n"
    assert hints(polling, "cli/tests/test_x.py") == []
    assert hints(waiting, "cli/tests/test_x.py") == [HintId.TEST_SEAM]


def test_a_clock_handed_to_a_call_as_its_now_is_the_form_the_hint_asks_for() -> None:
    handed = "from datetime import UTC, datetime\n\ndef f():\n    return g(now=datetime.now(UTC), finished_at=datetime.now(UTC))\n"
    kept = "from datetime import UTC, datetime\n\ndef f():\n    return g(label=datetime.now(UTC))\n"
    assert hints(handed) == []
    assert hints(kept) == [HintId.AMBIENT_READ]


def test_a_generated_id_is_not_an_ambient_read() -> None:
    assert hints("import uuid\n\ndef f():\n    return uuid.uuid4().hex\n") == []


def test_a_sleep_inside_a_fake_s_handler_is_the_behavior_it_simulates() -> None:
    handler = "import time\n\ndef test_a():\n    def handle(request):\n        time.sleep(0.2)\n    assert handle\n"
    method = "import time\n\nclass Slow:\n    def handle(self):\n        time.sleep(0.2)\n"
    assert hints(handler, "cli/tests/test_x.py") == []
    assert hints(method, "cli/tests/test_x.py") == []


def test_configuring_a_real_resource_fixture_or_a_double_s_own_dict_is_not_a_seam() -> None:
    resource = "def test_a(db_conn):\n    db_conn.autocommit = True\n"
    own = "class Fake:\n    def __init__(self):\n        self.__dict__['x'] = 1\n"
    armed = "def test_a(backend):\n    backend.fail_reap = True\n"
    assert hints(resource, "cli/tests/test_x.py") == []
    assert hints(own, "cli/tests/test_x.py") == []
    assert hints(armed, "cli/tests/test_x.py") == [HintId.TEST_SEAM]


def test_raising_set_by_a_value_a_dict_or_a_position_is_still_a_tolerated_miss() -> None:
    variable = "def test_a(monkeypatch, flag):\n    monkeypatch.setattr('a.b', 1, raising=flag)\n"
    spread = "def test_a(monkeypatch):\n    monkeypatch.setattr('a.b', 1, **{'raising': False})\n"
    positional = "def test_a(monkeypatch, o):\n    monkeypatch.setattr(o, 'x', 1, False)\n"
    strict = "def test_a(monkeypatch, o):\n    monkeypatch.setattr(o, 'x', 1, raising=True)\n"
    delenv = "def test_a(monkeypatch):\n    monkeypatch.delenv('A', raising=False)\n"
    for source in (variable, spread, positional):
        assert hints(source, "cli/tests/test_x.py") == [HintId.TEST_SEAM], source
    assert hints(strict, "cli/tests/test_x.py") == []
    assert hints(delenv, "cli/tests/test_x.py") == []


def test_a_wildcard_case_fails_only_when_it_always_ends_in_a_failure() -> None:
    nested = (
        "def f(x, y):\n    match x:\n        case 1:\n            return 1\n"
        "        case _:\n            if y:\n                raise ValueError(x)\n            return 0\n"
    )
    returned = (
        "from typing import assert_never\n\ndef f(x: int):\n    match x:\n        case 1:\n"
        "            return 1\n        case _:\n            return assert_never(x)\n"
    )
    assert hints(nested) == [HintId.WILDCARD_CASE]
    assert hints(returned) == []


def test_a_selector_guarded_in_its_own_body_is_fine_but_not_by_a_nested_def() -> None:
    guarded = "def f(action: str):\n    if action == 'a':\n        return 1\n    raise ValueError(action)\n"
    nested = (
        "def f(action: str):\n    def fail():\n        raise ValueError(action)\n"
        "    if action == 'a':\n        return 1\n    return 2\n"
    )
    assert hints(guarded) == []
    assert hints(nested) == [HintId.WILDCARD_CASE]


def test_none_fallback_sees_a_named_default_a_truthy_test_and_an_attribute_target() -> None:
    named = "def default_fetch(): ...\n\ndef f(fetch=None):\n    fetch = fetch or default_fetch\n    return fetch\n"
    imported = "from lib.mod import default_fetch\n\ndef f(fetch=None):\n    return fetch or default_fetch\n"
    constant = "from lib.mod import DEFAULT_LIMIT\n\ndef f(limit=None):\n    return limit or DEFAULT_LIMIT\n"
    truthy = "def f(client=None):\n    return client if client else Client()\n"
    attribute = "class A:\n    def __init__(self, client=None):\n        if client is None:\n            self.client = Client()\n"
    annotated = "def f(client=None):\n    if client is None:\n        client: Client = make()\n    return client\n"
    for source in (named, imported, truthy, attribute, annotated):
        assert hints(source) == [HintId.NONE_FALLBACK], source
    assert hints(constant) == []


def test_a_signature_over_several_lines_is_judged_by_any_added_line_of_the_parameter() -> None:
    source = (
        "def f(\n    a: int,\n    run: Callable[\n        [], int\n    ] = int,\n):\n    return a\n"
    )
    assert hints(source) == [HintId.CALLABLE_DEFAULT]
    only_default = sites(source, added=frozenset({5}))
    assert [s.hint for s in only_default] == [HintId.CALLABLE_DEFAULT]
    assert sites(source, added=frozenset({1})) == []


def test_an_aliased_callable_import_is_still_a_callable() -> None:
    source = "from collections.abc import Callable as Cb\n\ndef f(run: Cb[[], int] = int):\n    return run\n"
    assert hints(source) == [HintId.CALLABLE_DEFAULT]


def test_freezing_a_class_that_already_held_a_list_is_asked() -> None:
    source = "from dataclasses import dataclass\n\n@dataclass(frozen=True)\nclass V:\n    items: list[str]\n"
    assert [s.line for s in sites(source, added=frozenset({3}))] == [5]
    assert sites(source, added=frozenset({1})) == []


def test_an_interval_clock_asks_only_where_the_function_also_sleeps() -> None:
    duration = "import time\n\ndef f():\n    started = time.monotonic()\n    return time.monotonic() - started\n"
    polling = (
        "import time\n\ndef f():\n    deadline = time.monotonic() + 3\n"
        "    while time.monotonic() < deadline:\n        time.sleep(0.1)\n"
    )
    asyncio_poll = (
        "import asyncio\nimport time\n\nasync def f():\n    t = time.perf_counter()\n"
        "    await asyncio.sleep(1)\n    return t\n"
    )
    nested = (
        "import time\n\ndef f():\n    def g():\n        return time.monotonic()\n"
        "    time.sleep(1)\n    return g\n"
    )
    assert hints(duration) == []
    assert [s.hint for s in sites(polling)] == [HintId.AMBIENT_READ] * 2
    assert hints(asyncio_poll) == [HintId.AMBIENT_READ]
    assert hints(nested) == []


def test_the_wall_clock_always_asks() -> None:
    wall = "import time\nfrom datetime import UTC, datetime\n\ndef f():\n    return time.time(), datetime.now(UTC)\n"
    assert hints(wall) == [HintId.AMBIENT_READ, HintId.AMBIENT_READ]
