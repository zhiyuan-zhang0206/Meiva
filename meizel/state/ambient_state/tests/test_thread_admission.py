from __future__ import annotations

import ast
from pathlib import Path

import pytest

from meizel.state import ambient_state
from meizel.state.ambient_state.module import Tree
from meizel.state.ambient_state.tests.conftest import SCOPE

SOURCE = """
import threading

class Owner:
    def __init__(self, work, report):
        self._work = work
        self._report = report
        self._stop_requested = threading.Event()
        self._finished = threading.Event()
        self._error = None
        self._lock = threading.Lock()
        self._thread = None

    def start(self):
        with self._lock:
            if self._stop_requested.is_set():
                return False
            if self._thread is not None:
                raise RuntimeError('already started')
            self._thread = threading.Thread(target=self._run, daemon=True)
            thread = self._thread
        thread.start()
        return True

    def _run(self):
        try:
            self._work(self._stop_requested)
        except BaseException as error:
            self._error = error
            self._report(error)
        finally:
            self._finished.set()

    def request_stop(self):
        with self._lock:
            self._stop_requested.set()

    def stop(self, timeout=1):
        self.request_stop()
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=timeout)
        unfinished = thread.is_alive()
        if self._error is not None:
            raise self._error
        return not unfinished
"""


def sites(source: str, root: Path) -> dict[str, list[int]]:
    return ambient_state.measure(ast.parse(source), "base/worker.py", _tree(root), SCOPE)


def test_deferred_constructor_has_a_single_locked_admission(tmp_path: Path) -> None:
    assert sites(SOURCE, tmp_path) == {}


def test_direct_retained_handle_can_start_outside_the_lock(tmp_path: Path) -> None:
    source = SOURCE.replace("            thread = self._thread\n", "", 1).replace(
        "        thread.start()", "        self._thread.start()"
    )
    assert sites(source, tmp_path) == {}


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("import threading", "import threading\nthreading = factories"),
        (
            "    def __init__(self, work, report):",
            "    def __init__(self, work, report, factories):\n"
            "        match factories:\n            case threading:\n                pass",
        ),
        (
            "    def start(self):",
            "    def start(self, factories):\n"
            "        match factories:\n            case threading:\n                pass",
        ),
    ],
)
def test_visible_factory_rebindings_do_not_prove_native_synchronization(
    tmp_path: Path, before: str, after: str
) -> None:
    source = SOURCE.replace(before, after)
    assert source != SOURCE
    assert "base/worker.py::thread:Owner.start" in sites(source, tmp_path)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("self._thread = None", "self._thread = other"),
        ("self._lock = threading.Lock()", "self._lock = lock"),
        ("self._stop_requested = threading.Event()", "self._stop_requested = event"),
        ("if self._stop_requested.is_set():\n                return False", "pass"),
        (
            "if self._thread is not None:\n                raise RuntimeError('already started')",
            "pass",
        ),
        ("return False", "pass"),
        ("raise RuntimeError('already started')", "pass"),
        ("if self._stop_requested.is_set()", "if not self._stop_requested.is_set()"),
        ("if self._thread is not None", "if self._thread is None"),
        (
            "            thread = self._thread",
            "            thread = self._thread\n        thread = other",
        ),
        ("        thread.start()", "        del thread\n        thread.start()"),
        ("        thread.start()", "        mutate(self)\n        thread.start()"),
        ("    def start(self):", "    def start(self):\n        global thread"),
        ("    def start(self):", "    def start(self, threading):"),
        (
            "    def __init__(self, work, report):",
            "    def __init__(self, work, report, threading):",
        ),
        (
            "        with self._lock:\n            if",
            "        threading = other\n        with self._lock:\n            if",
        ),
        ("        thread.start()", "        self._thread = other\n        thread.start()"),
        ("        thread.start()", "        thread._target = other\n        thread.start()"),
        ("        thread.start()", "        self._lock = other\n        thread.start()"),
        ("        thread.start()", "        self._stop_requested = other\n        thread.start()"),
        (
            "        thread.start()",
            "        setattr(self, '_thread', other)\n        thread.start()",
        ),
        (
            "        thread.start()",
            "        self.__dict__['_thread'] = other\n        thread.start()",
        ),
        ("        thread.start()", "        self._stop_requested.clear()\n        thread.start()"),
        (
            "        thread.start()",
            "        alias = self._stop_requested\n        alias.clear()\n        thread.start()",
        ),
        (
            "        with self._lock:\n            if",
            "        with self._lock:\n            self._lock.release()\n            if",
        ),
        ("thread.join(timeout=timeout)", "thread.join()"),
        ("thread.is_alive()", "False"),
        ("self.request_stop()", "pass"),
        ("raise self._error", "return False"),
        ("self._report(error)", "pass"),
        ("except BaseException as error", "except Exception as error"),
        ("self._error = error", "self._error = None"),
        (
            "    def request_stop(self):\n        with self._lock:\n            self._stop_requested.set()",
            "    def request_stop(self):\n        self._stop_requested.set()",
        ),
        (
            "    def request_stop(self):\n        with self._lock:",
            "    def request_stop(self):\n        with other:",
        ),
        (
            "            self._thread = threading.Thread(target=self._run, daemon=True)",
            "            self._thread = threading.Thread(target=self._run, daemon=True)\n            self._thread = threading.Thread(target=self._run, daemon=True)",
        ),
    ],
)
def test_decorative_or_incomplete_admission_is_rejected(
    tmp_path: Path, before: str, after: str
) -> None:
    source = SOURCE.replace(before, after)
    assert source != SOURCE
    assert "base/worker.py::thread:Owner.start" in sites(source, tmp_path)


def test_guards_outside_the_lock_cannot_dominate_construction(tmp_path: Path) -> None:
    guards = """        with self._lock:
            if self._stop_requested.is_set():
                return False
            if self._thread is not None:
                raise RuntimeError('already started')
"""
    outside = """        if self._stop_requested.is_set():
            return False
        if self._thread is not None:
            raise RuntimeError('already started')
        with self._lock:
"""
    assert sites(SOURCE.replace(guards, outside), tmp_path)


def test_guards_after_construction_do_not_close_admission(tmp_path: Path) -> None:
    constructor = "            self._thread = threading.Thread(target=self._run, daemon=True)\n"
    source = SOURCE.replace(constructor, "").replace(
        "        with self._lock:\n", "        with self._lock:\n" + constructor, 1
    )
    assert sites(source, tmp_path)


@pytest.mark.parametrize(
    "extra",
    [
        "mutate((self,))",
        "mutate([self])",
        "mutate({'owner': self})",
        "callback(self._stop_requested)",
    ],
)
def test_unknown_effects_between_guards_and_construction_stay_unproved(
    tmp_path: Path, extra: str
) -> None:
    constructor = "            self._thread = threading.Thread(target=self._run, daemon=True)"
    assert sites(SOURCE.replace(constructor, "            " + extra + "\n" + constructor), tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        "clear = self._stop_requested.clear\n        clear()",
        "setter = self._stop_requested.set\n        setter()",
        "callback(self._stop_requested)",
        "mutate((self,))",
        "mutate([self])",
        "mutate({'owner': self})",
        "owner = (self,)\n        mutate(owner)",
        "return (self,)",
        "self.__init__(self._work, self._report)",
        "reset = self.__init__\n        reset(self._work, self._report)",
        "def reset():\n            self._thread = None\n        reset()",
    ],
)
def test_other_methods_cannot_escape_or_reset_admission(tmp_path: Path, body: str) -> None:
    assert sites(SOURCE + "\n    def reopen(self):\n        " + body + "\n", tmp_path)


# meiva:note
# The files a test wrote under `root`, as the tree the rule is handed.
def _tree(root: Path) -> Tree:
    files = {path.relative_to(root).as_posix() for path in root.rglob("*.py")}
    dirs = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_dir()}
    return Tree(files, lambda rel: (root / rel).read_text(encoding="utf-8"), dirs)
