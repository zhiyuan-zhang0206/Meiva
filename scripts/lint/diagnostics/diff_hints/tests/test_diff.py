from __future__ import annotations

from scripts.lint.diagnostics.diff_hints.diff import parse_diff

PATCH = """\
diff --git a/cli/a.py b/cli/a.py
index 111..222 100644
--- a/cli/a.py
+++ b/cli/a.py
@@ -3,0 +4,2 @@ def f():
+import time
+x = time.time()
@@ -10 +11,0 @@ def g():
-def test_gone():
diff --git a/cli/new.py b/cli/new.py
new file mode 100644
index 0000000..333
--- /dev/null
+++ b/cli/new.py
@@ -0,0 +1,2 @@
+a = 1
+b = 2
diff --git a/cli/old_test.py b/cli/old_test.py
deleted file mode 100644
index 444..0000000
--- a/cli/old_test.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def test_one():
-    pass
"""


def test_added_lines_and_text_are_read_per_file() -> None:
    changes = parse_diff(PATCH)
    first = changes[0]
    assert first.path == "cli/a.py"
    assert first.added == frozenset({4, 5})
    assert first.added_text == ("import time", "x = time.time()")
    assert [(r.old_line, r.text) for r in first.removed] == [(10, "def test_gone():")]
    assert not first.deleted


def test_a_new_file_adds_every_line() -> None:
    created = parse_diff(PATCH)[1]
    assert created.path == "cli/new.py"
    assert created.added == frozenset({1, 2})


def test_a_deleted_file_keeps_its_old_path_and_removed_lines() -> None:
    gone = parse_diff(PATCH)[2]
    assert gone.deleted
    assert gone.path == "cli/old_test.py"
    assert [(r.old_line, r.text) for r in gone.removed] == [(1, "def test_one():"), (2, "    pass")]


def test_a_removed_line_that_looks_like_a_header_stays_content() -> None:
    patch = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,0 @@\n"
        "--- not a header\n-- also content\n"
    )
    (change,) = parse_diff(patch)
    assert [r.text for r in change.removed] == ["-- not a header", "- also content"]
    assert change.path == "x.py"


def test_an_empty_diff_is_no_change() -> None:
    assert parse_diff("") == ()
