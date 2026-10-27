from __future__ import annotations

import pytest

from meizel.contracts.analysis import render_policy
from meizel.tests.synthetic import RepoFactory, analysis_of, diagnostics, failures, run

ROOT = '[roles]\napplication = ["app"]\ntests = ["tests"]\n'


def entropy(make_repo: RepoFactory, source: str, path: str = "app/mod.py") -> list[str]:
    root = make_repo(
        {
            "meizel.toml": ROOT,
            "meizel.policy.toml": render_policy({}, {}),
            "app/__init__.py": "",
            "tests/__init__.py": "",
            path: source,
        }
    )
    return [site.what for site in analysis_of(root).scans[path].entropy]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "import time\n\ndef run_id():\n    return f'{time.monotonic_ns():x}'\n",
            ["time.monotonic_ns in an f-string"],
        ),
        (
            "from time import perf_counter_ns as tick\n\ndef key():\n    return f'k-{tick()}'\n",
            ["time.perf_counter_ns in an f-string"],
        ),
        (
            "import time\nfrom uuid import NAMESPACE_OID, uuid5\n\ndef make():\n"
            "    return uuid5(NAMESPACE_OID, f'outbox-{time.perf_counter_ns()}')\n",
            ["time.perf_counter_ns in an f-string"],
        ),
        (
            "import time\n\ndef draw():\n"
            "    return ((time.perf_counter_ns() * 1103515245 + 12345) & 0xFFFFFFFF) / 0x100000000\n",
            ["time.perf_counter_ns in `&`"],
        ),
        (
            "import time\n\ndef pick(n):\n    return time.monotonic_ns() % n\n",
            ["time.monotonic_ns in `%`"],
        ),
        (
            "import time\n\ndef name():\n    stamp = time.monotonic_ns()\n    return f'{stamp}.json'\n",
            ["time.monotonic_ns in an f-string"],
        ),
        (
            "import time\n\ndef digits():\n    value = time.monotonic_ns()\n    return str(value)\n",
            ["time.monotonic_ns in str"],
        ),
        (
            "import time\n\ndef go(probe):\n    return probe(run_id=time.monotonic_ns())\n",
            ["time.monotonic_ns in run_id="],
        ),
        (
            "import random\nimport time\n\ndef rng():\n    return random.Random(time.perf_counter())\n",
            ["time.perf_counter in random.Random"],
        ),
        (
            "class A:\n    def tag(self):\n        return f'{id(self):x}'\n",
            ["id in an f-string"],
        ),
        (
            "import itertools\n\nclass A:\n    def __init__(self):\n        self._ids = itertools.count()\n\n"
            "    def next_id(self):\n        return f'op-{next(self._ids)}'\n",
            ["itertools.count in an f-string"],
        ),
        (
            "from uuid import UUID\n\nclass A:\n    def key(self):\n        return UUID(int=id(self))\n",
            ["id in uuid.UUID"],
        ),
        (
            "import time\nimport uuid\n\ndef f():\n    return uuid.UUID(int=time.monotonic_ns())\n",
            ["time.monotonic_ns in uuid.UUID"],
        ),
        (
            "import time\n\ndef f():\n    return 'run-{}'.format(time.monotonic_ns())\n",
            ["time.monotonic_ns in .format()"],
        ),
        (
            "import time\n\ndef f():\n    return 'run-{n}'.format(n=time.perf_counter())\n",
            ["time.perf_counter in .format()"],
        ),
        (
            "import time\n\ndef f():\n    return time.monotonic().hex()\n",
            ["time.monotonic in .hex()"],
        ),
        (
            "import time\n\ndef f():\n    return time.monotonic_ns().to_bytes(8, 'big')\n",
            ["time.monotonic_ns in .to_bytes()"],
        ),
        (
            "import time\n\ndef f():\n    return hash(time.monotonic_ns())\n",
            ["time.monotonic_ns in hash"],
        ),
        (
            "import time\n\ndef f():\n    return time.monotonic_ns() << 3\n",
            ["time.monotonic_ns in `<<`"],
        ),
        (
            "import time\n\ndef outer():\n    def inner():\n        return f'{time.monotonic_ns()}'\n"
            "    return inner\n",
            ["time.monotonic_ns in an f-string"],
        ),
        (
            "import time\n\nasync def f():\n    return f'{time.monotonic_ns()}'\n",
            ["time.monotonic_ns in an f-string"],
        ),
        (
            "import time\n\nnew_id = lambda: f'{time.monotonic_ns()}'\n",
            ["time.monotonic_ns in an f-string"],
        ),
    ],
)
def test_a_reading_that_is_not_entropy_is_spent_as_identity_or_randomness(
    make_repo: RepoFactory, source: str, expected: list[str]
) -> None:
    assert entropy(make_repo, source) == expected


@pytest.mark.parametrize(
    "source",
    [
        "import time\n\ndef f():\n    started = time.monotonic()\n    return f'{time.monotonic() - started:.1f}s'\n",
        "import time\n\ndef f(work):\n    started = time.monotonic()\n    work()\n"
        "    return int((time.monotonic() - started) * 1000)\n",
        "import time\n\ndef f(work):\n    start = time.perf_counter_ns()\n    work()\n"
        "    elapsed = time.perf_counter_ns() - start\n    return f'{elapsed}ns'\n",
        "import time\n\ndef f(limit):\n    deadline = time.monotonic() + limit\n"
        "    while time.monotonic() < deadline:\n        pass\n",
        "import time\n\ndef f(cache, key):\n    cache[key] = time.monotonic()\n",
        "import itertools\n\ncounter = itertools.count()\n\ndef sample(every):\n"
        "    return next(counter) % every == 0\n",
        "def f(seen, item):\n    seen.add(id(item))\n    return id(item) in seen\n",
        "class A:\n    def __repr__(self):\n        return f'<A at {id(self):#x}>'\n",
        "import time\n\ndef f():\n    return f'{time.time()}'\n",
        "def f(time):\n    return f'{time.monotonic_ns()}'\n",
    ],
)
def test_a_duration_a_deadline_an_identity_set_and_a_sampler_spend_nothing(
    make_repo: RepoFactory, source: str
) -> None:
    assert entropy(make_repo, source) == []


def test_the_loop_clock_spent_as_an_identifier_is_found(make_repo: RepoFactory) -> None:
    source = (
        "import asyncio\n\ndef run_id():\n    return f'{asyncio.get_running_loop().time():x}'\n"
    )
    assert entropy(make_repo, source) == ["loop.time in an f-string"]


def test_a_test_file_records_nothing(make_repo: RepoFactory) -> None:
    source = "import time\n\ndef test_a():\n    assert f'{time.monotonic_ns()}'\n"
    assert entropy(make_repo, source, "tests/test_x.py") == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "import time\n\ndef _monotonic() -> float:\n    return time.monotonic()\n\n\n"
            "def wait(limit):\n    return _monotonic() + limit\n",
            ["interval-wrapper _monotonic"],
        ),
        (
            "from time import perf_counter\n\ndef _perf_counter():\n    '''The clock.'''\n"
            "    return perf_counter()\n",
            ["interval-wrapper _perf_counter"],
        ),
        (
            "import time\n\n_monotonic = time.monotonic\n\n\ndef wait(limit):\n"
            "    return _monotonic() + limit\n",
            ["interval-wrapper _monotonic"],
        ),
        (
            "import time\n\n_now = lambda: time.monotonic()\n\n\ndef wait(limit):\n    return _now() + limit\n",
            ["interval-wrapper _now"],
        ),
        (
            "import time\nfrom functools import partial\n\n_now = partial(time.monotonic)\n\n\n"
            "def wait(limit):\n    return _now() + limit\n",
            ["interval-wrapper _now"],
        ),
        (
            "import time\n\nclass Gate:\n    def _now(self) -> float:\n        return time.monotonic()\n\n"
            "    def wait(self, limit):\n        return self._now() + limit\n",
            ["interval-wrapper _now"],
        ),
        (
            "import asyncio\n\ndef _now() -> float:\n    return asyncio.get_running_loop().time()\n\n\n"
            "def wait(limit):\n    return _now() + limit\n",
            ["interval-wrapper _now"],
        ),
        (
            "import time\nfrom time import monotonic as clock\n\n_now = clock\n\n\ndef wait(limit):\n"
            "    return _now() + limit\n",
            ["interval-wrapper _now"],
        ),
    ],
)
def test_a_private_wrapper_over_an_interval_clock_that_nothing_injects_is_found(
    make_repo: RepoFactory, source: str, expected: list[str]
) -> None:
    assert entropy(make_repo, source) == expected


@pytest.mark.parametrize(
    "source",
    [
        "import time\nfrom collections.abc import Callable\n\ndef _monotonic() -> float:\n"
        "    return time.monotonic()\n\n\ndef wait(limit, now: Callable[[], float] = _monotonic):\n"
        "    return now() + limit\n",
        "import time\n\ndef _monotonic() -> float:\n    return time.monotonic()\n\n\n"
        "def build():\n    return Waiter(now=_monotonic)\n",
        "import time\n\ndef monotonic() -> float:\n    return time.monotonic()\n",
        "import time\n\ndef _since(start: float) -> float:\n    return time.monotonic() - start\n",
        "import time\n\ndef _stamp() -> float:\n    return time.time()\n",
        "import time\n\n_now = time.monotonic\n\n\ndef build():\n    return Waiter(now=_now)\n",
        "import time\n\n_now = lambda: time.monotonic()\n\n\ndef wait(now=_now):\n    return now()\n",
        "import time\n\nclass Gate:\n    def _now(self) -> float:\n        return time.monotonic()\n\n"
        "    def build(self):\n        return Waiter(now=self._now)\n",
        "import time\n\nclass Gate:\n    def _now(self, scale: float) -> float:\n        return time.monotonic() * scale\n",
        "import asyncio\n\nasync def waited(loop):\n    started = loop.time()\n    return loop.time() - started\n",
    ],
)
def test_a_wrapper_the_module_injects_a_public_one_and_other_shapes_are_left_alone(
    make_repo: RepoFactory, source: str
) -> None:
    assert entropy(make_repo, source) == []


def test_a_use_is_debt_a_new_one_fails_and_a_strict_file_has_none(make_repo: RepoFactory) -> None:
    source = "import time\n\ndef run_id():\n    return f'{time.monotonic_ns():x}'\n"
    root = make_repo(
        {
            "meizel.toml": ROOT,
            "meizel.policy.toml": render_policy({}, {}),
            "app/__init__.py": "",
            "tests/__init__.py": "",
            "app/meizel.toml": "",
            "app/mod.py": source,
        }
    )
    found = diagnostics(root, "fake-entropy")
    assert [(d.line, d.debt.key if d.debt else None) for d in found] == [
        (4, "mod.py#time.monotonic_ns in an f-string")
    ]
    assert "random" in found[0].hints[0]
    assert ("fake-entropy", "app/mod.py") in failures(root)
    assert run(root, "debt", "record", ".") == 0
    assert ("fake-entropy", "app/mod.py") not in failures(root)
    (root / "app/mod.py").write_text(source + "\ndef again():\n    return f'{id(0)}'\n")
    assert ("fake-entropy", "app/mod.py") in failures(root)
    (root / "app/meizel.toml").write_text('[purity]\n"mod.py" = "strict"\n')
    assert [d.debt for d in diagnostics(root, "fake-entropy")] == [None, None]
