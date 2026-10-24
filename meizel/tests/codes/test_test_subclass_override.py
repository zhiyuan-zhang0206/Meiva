from __future__ import annotations

import pytest

from meizel.tests.synthetic import RepoFactory, analysis_of

LIB = {
    "lib/__init__.py": "from lib.engine import Engine as Engine\n",
    "lib/engine.py": (
        "class Engine:\n"
        "    def __init__(self) -> None:\n"
        "        self.n = 0\n\n"
        "    def run(self) -> int:\n"
        "        return 1\n\n"
        "    def stop(self) -> None: ...\n"
    ),
    "lib/turbo.py": (
        "from lib.engine import Engine\n\n"
        "class Turbo(Engine):\n"
        "    def boost(self) -> int:\n"
        "        return 2\n"
    ),
    "lib/port.py": (
        "from abc import ABC, abstractmethod\n"
        "from typing import Protocol\n\n"
        "class Port(Protocol):\n"
        "    def send(self) -> None: ...\n\n"
        "class Base(ABC):\n"
        "    @abstractmethod\n"
        "    def go(self) -> None: ...\n\n"
        "class Marked:\n"
        "    @abstractmethod\n"
        "    def mark(self) -> None: ...\n"
    ),
    "app/__init__.py": "",
    "app/tests/__init__.py": "",
    "app/tests/fakes.py": "class Fake:\n    def run(self) -> int:\n        return 0\n",
}


def overrides(make_repo: RepoFactory, source: str, path: str = "app/tests/test_x.py") -> list[str]:
    root = make_repo({**LIB, path: source})
    return sorted(
        d.debt.key
        for d in analysis_of(root).diagnostics
        if d.debt is not None and d.path == path and d.code.value == "test-subclass-override"
    )


@pytest.mark.parametrize(
    ("source", "path", "expected"),
    [
        (
            "from lib.engine import Engine\n\nclass Fast(Engine):\n    def run(self) -> int:\n        return 9\n",
            "app/tests/test_x.py",
            ["test_x.py#subclass lib.engine.Engine.run"],
        ),
        (
            "from lib.engine import Engine\n\nclass Fast(Engine):\n    def run(self) -> int:\n        return 9\n",
            "app/tests/support.py",
            ["support.py#subclass lib.engine.Engine.run"],
        ),
        (
            "from lib import Engine\n\nclass Fast(Engine):\n    def stop(self) -> None: ...\n",
            "app/tests/test_x.py",
            ["test_x.py#subclass lib.engine.Engine.stop"],
        ),
        (
            "from lib.turbo import Turbo\n\nclass Fast(Turbo):\n    def run(self) -> int:\n        return 9\n    def boost(self) -> int:\n        return 3\n",
            "app/tests/test_x.py",
            [
                "test_x.py#subclass lib.turbo.Turbo.boost",
                "test_x.py#subclass lib.turbo.Turbo.run",
            ],
        ),
        (
            "import lib.engine as eng\n\ndef test_a():\n    class Fast(eng.Engine):\n        def run(self) -> int:\n            return 9\n",
            "app/tests/test_x.py",
            ["test_x.py#subclass lib.engine.Engine.run"],
        ),
        (
            "from lib.engine import Engine\n\nclass A(Engine):\n    def run(self) -> int:\n        return 1\n\nclass B(Engine):\n    def run(self) -> int:\n        return 2\n",
            "app/tests/test_x.py",
            [
                "test_x.py#subclass lib.engine.Engine.run",
                "test_x.py#subclass lib.engine.Engine.run",
            ],
        ),
    ],
)
def test_a_test_class_that_overrides_a_production_method_is_recorded_by_the_method(
    make_repo: RepoFactory, source: str, path: str, expected: list[str]
) -> None:
    assert overrides(make_repo, source, path) == expected


@pytest.mark.parametrize(
    ("source", "path"),
    [
        (
            "from lib.engine import Engine\n\nclass Fast(Engine):\n    def extra(self) -> int:\n        return 9\n",
            "app/tests/test_x.py",
        ),
        (
            "from lib.engine import Engine\n\nclass Fast(Engine):\n    def __init__(self) -> None:\n        super().__init__()\n",
            "app/tests/test_x.py",
        ),
        ("from lib.engine import Engine\n\nclass Fast(Engine):\n    pass\n", "app/tests/test_x.py"),
        (
            "from lib.port import Port\n\nclass Fake(Port):\n    def send(self) -> None: ...\n",
            "app/tests/test_x.py",
        ),
        (
            "from lib.port import Base\n\nclass Fake(Base):\n    def go(self) -> None: ...\n",
            "app/tests/test_x.py",
        ),
        (
            "from lib.port import Marked\n\nclass Fake(Marked):\n    def mark(self) -> None: ...\n",
            "app/tests/test_x.py",
        ),
        (
            "from app.tests.fakes import Fake\n\nclass Louder(Fake):\n    def run(self) -> int:\n        return 5\n",
            "app/tests/test_x.py",
        ),
        (
            "class Base:\n    def run(self) -> int:\n        return 1\n\nclass Sub(Base):\n    def run(self) -> int:\n        return 2\n",
            "app/tests/test_x.py",
        ),
        (
            "import threading\n\nclass T(threading.Thread):\n    def run(self) -> None: ...\n",
            "app/tests/test_x.py",
        ),
        (
            "from lib.engine import Engine\n\nclass Fast(Engine):\n    def run(self) -> int:\n        return 9\n",
            "app/prod.py",
        ),
    ],
)
def test_subclassing_that_is_not_a_seam_is_not_recorded(
    make_repo: RepoFactory, source: str, path: str
) -> None:
    assert overrides(make_repo, source, path) == []
